// The least DOM react-dom's client renderer needs, for a test that mounts a component and changes
// its props: nodes, text, attributes and listeners, and nothing it does not call. `install` puts it
// on the global object and returns what takes it off again, so no other test sees it.

class MiniNode {
  childNodes: MiniNode[] = [];
  parentNode: MiniNode | null = null;
  nodeType = 1;
  nodeName = "";
  listeners: Record<string, ((event: unknown) => void)[]> = {};

  constructor(public ownerDocument: MiniDocument | null) {}

  get firstChild(): MiniNode | null {
    return this.childNodes[0] ?? null;
  }

  get lastChild(): MiniNode | null {
    return this.childNodes[this.childNodes.length - 1] ?? null;
  }

  get nextSibling(): MiniNode | null {
    const parent = this.parentNode;
    if (parent === null) return null;
    return parent.childNodes[parent.childNodes.indexOf(this) + 1] ?? null;
  }

  appendChild(child: MiniNode): MiniNode {
    child.parentNode?.removeChild(child);
    this.childNodes.push(child);
    child.parentNode = this;
    return child;
  }

  insertBefore(child: MiniNode, before: MiniNode | null): MiniNode {
    if (before === null) return this.appendChild(child);
    child.parentNode?.removeChild(child);
    this.childNodes.splice(this.childNodes.indexOf(before), 0, child);
    child.parentNode = this;
    return child;
  }

  removeChild(child: MiniNode): MiniNode {
    this.childNodes.splice(this.childNodes.indexOf(child), 1);
    child.parentNode = null;
    return child;
  }

  addEventListener(type: string, listener: (event: unknown) => void): void {
    this.listeners[type] ??= [];
    this.listeners[type].push(listener);
  }

  removeEventListener(): void {}

  get textContent(): string {
    return this.childNodes.map((child) => child.textContent).join("");
  }

  set textContent(value: string) {
    this.childNodes = [];
    if (value !== "") this.appendChild(new MiniText(this.ownerDocument, value));
  }
}

class MiniText extends MiniNode {
  constructor(
    ownerDocument: MiniDocument | null,
    public nodeValue: string,
  ) {
    super(ownerDocument);
    this.nodeType = 3;
    this.nodeName = "#text";
  }

  override get textContent(): string {
    return this.nodeValue;
  }

  override set textContent(value: string) {
    this.nodeValue = value;
  }
}

export class MiniElement extends MiniNode {
  attributes: Record<string, string> = {};
  namespaceURI = "http://www.w3.org/1999/xhtml";
  tagName: string;
  style: Record<string, unknown> & {
    setProperty(key: string, value: string): void;
    removeProperty(key: string): void;
  };
  checked = false;
  disabled = false;

  constructor(ownerDocument: MiniDocument | null, tag: string) {
    super(ownerDocument);
    this.tagName = tag.toUpperCase();
    this.nodeName = this.tagName;
    const style: Record<string, unknown> = {};
    this.style = Object.assign(style, {
      setProperty: (key: string, value: string) => {
        style[key] = value;
      },
      removeProperty: (key: string) => {
        delete style[key];
      },
    });
  }

  setAttribute(key: string, value: string): void {
    this.attributes[key] = String(value);
  }

  getAttribute(key: string): string | null {
    return this.attributes[key] ?? null;
  }

  removeAttribute(key: string): void {
    delete this.attributes[key];
  }

  hasAttribute(key: string): boolean {
    return key in this.attributes;
  }

  /** Take focus: the document records it, so a test can ask where focus went. */
  focus(): void {
    if (this.ownerDocument !== null) this.ownerDocument.activeElement = this;
  }

  /** Only `:focus-visible` is answered: nothing here has keyboard modality, so it never matches. A
   *  selector this stub cannot answer fails the test rather than guessing. */
  matches(selector: string): boolean {
    if (selector === ":focus-visible") return false;
    throw new Error(`the mini DOM cannot match ${selector}`);
  }

  /** Nothing is laid out, so every element measures as an empty box at the origin. */
  getBoundingClientRect(): {
    width: number;
    height: number;
    top: number;
    left: number;
  } {
    return { width: 0, height: 0, top: 0, left: 0 };
  }
}

class MiniDocument extends MiniNode {
  body: MiniElement;
  documentElement: MiniElement;
  activeElement: MiniElement | null = null;
  defaultView: unknown;

  constructor() {
    super(null);
    this.ownerDocument = this;
    this.nodeType = 9;
    this.nodeName = "#document";
    this.documentElement = this.createElement("html");
    this.body = this.createElement("body");
    this.documentElement.appendChild(this.body);
    this.appendChild(this.documentElement);
  }

  createElement(tag: string): MiniElement {
    return new MiniElement(this, tag);
  }

  createElementNS(_namespace: string, tag: string): MiniElement {
    return new MiniElement(this, tag);
  }

  createTextNode(value: string): MiniText {
    return new MiniText(this, value);
  }
}

/** Every node beneath and including `node` that `matches`. */
export function findAll(
  node: MiniNode,
  matches: (node: MiniNode) => boolean,
): MiniNode[] {
  const found: MiniNode[] = [];
  const walk = (at: MiniNode) => {
    if (matches(at)) found.push(at);
    for (const child of at.childNodes) walk(child);
  };
  walk(node);
  return found;
}

/** Dispatch a click on `target` to the listeners React put on `root`, as a browser's would reach
 *  them by bubbling. */
export function click(root: MiniNode, target: MiniElement): void {
  if (target instanceof MiniElement && target.tagName === "INPUT") {
    target.checked = !target.checked;
  }
  const event = {
    type: "click",
    target,
    bubbles: true,
    cancelable: true,
    timeStamp: 0,
    defaultPrevented: false,
    preventDefault() {
      this.defaultPrevented = true;
    },
    stopPropagation() {},
    composedPath: () => [],
  };
  for (const listener of root.listeners.click ?? []) listener(event);
}

/** Put a mini DOM on the global object; returns its document and what removes it again. */
export function install(): { document: MiniDocument; uninstall: () => void } {
  const global = globalThis as Record<string, unknown>;
  const document = new MiniDocument();
  const added: Record<string, unknown> = {
    document,
    window: globalThis,
    HTMLIFrameElement: class {},
    HTMLElement: MiniElement,
    Node: MiniNode,
    // Lays nothing out, so it never reports a size: a component draws as before its first measurement.
    ResizeObserver: class {
      observe(): void {}
      unobserve(): void {}
      disconnect(): void {}
    },
    IS_REACT_ACT_ENVIRONMENT: true,
  };
  if (global.navigator === undefined) added.navigator = { userAgent: "bun" };
  if (global.addEventListener === undefined) added.addEventListener = () => {};
  if (global.removeEventListener === undefined)
    added.removeEventListener = () => {};
  const before = new Map(
    Object.keys(added).map((key) => [
      key,
      Object.getOwnPropertyDescriptor(global, key),
    ]),
  );
  for (const [key, value] of Object.entries(added)) {
    Object.defineProperty(global, key, {
      value,
      configurable: true,
      writable: true,
    });
  }
  document.defaultView = globalThis;
  return {
    document,
    uninstall: () => {
      for (const [key, descriptor] of before) {
        if (descriptor === undefined) delete global[key];
        else Object.defineProperty(global, key, descriptor);
      }
    },
  };
}
