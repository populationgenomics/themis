import {
  ModeOfInheritance,
  ModeOfInheritanceSchema,
} from "@/gen/themis/svcv4/models/svcv4_pb";

// The registered mode of inheritance in its three spellings: the enum member the code routes on, the
// column value `curation.variants.mode_of_inheritance` stores (the member name without its prefix),
// and the words a manager and a curator read.

const PREFIX = "MODE_OF_INHERITANCE_";

/** Every mode a registration may state, in the order the form offers them: the label a picker
 *  shows, and the form the mode takes inside a line of text. */
export const MODES: readonly {
  mode: ModeOfInheritance;
  label: string;
  inline: string;
}[] = [
  {
    mode: ModeOfInheritance.AUTOSOMAL_DOMINANT,
    label: "Autosomal dominant",
    inline: "autosomal dominant",
  },
  {
    mode: ModeOfInheritance.AUTOSOMAL_RECESSIVE,
    label: "Autosomal recessive",
    inline: "autosomal recessive",
  },
  {
    mode: ModeOfInheritance.SEMIDOMINANT,
    label: "Semi-dominant",
    inline: "semi-dominant",
  },
  {
    mode: ModeOfInheritance.X_LINKED_DOMINANT,
    label: "X-linked dominant",
    inline: "X-linked dominant",
  },
  {
    mode: ModeOfInheritance.X_LINKED_RECESSIVE,
    label: "X-linked recessive",
    inline: "X-linked recessive",
  },
];

function entry(mode: ModeOfInheritance): (typeof MODES)[number] {
  const found = MODES.find((m) => m.mode === mode);
  if (found === undefined) {
    throw new Error(`${mode} is not a mode a registration states`);
  }
  return found;
}

/** The mode as a line of text names it: "autosomal dominant", "X-linked recessive". */
export function inlineMode(mode: ModeOfInheritance): string {
  return entry(mode).inline;
}

/** The member's proto3-JSON name, which the register request carries. */
export function modeName(mode: ModeOfInheritance): string {
  entry(mode);
  return ModeOfInheritanceSchema.value[mode].name;
}

/** The member a proto3-JSON name stands for, or undefined for a name that is not one of the five. */
export function modeFromName(name: string): ModeOfInheritance | undefined {
  const value = ModeOfInheritanceSchema.values.find((v) => v.name === name);
  return MODES.find((m) => m.mode === value?.number)?.mode;
}

/** The value the column stores for the mode. */
export function modeColumn(mode: ModeOfInheritance): string {
  return modeName(mode).slice(PREFIX.length);
}

/** The member a stored column value names. Throws on anything else: the column's CHECK admits the
 *  five names only, so another value is a schema this code was not written against. */
export function modeFromColumn(column: string): ModeOfInheritance {
  const mode = modeFromName(`${PREFIX}${column}`);
  if (mode === undefined) {
    throw new Error(
      `curation.variants holds an unknown mode of inheritance: ${column}`,
    );
  }
  return mode;
}
