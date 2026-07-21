import { type File, Storage } from "@google-cloud/storage";
import type { GcsConfig } from "./config";

// GCS-direct working-document reads. The store writes each snapshot to
// `<analysis_id>/versions/<n>` in the working-document bucket (the object body IS
// the markdown); the BFF reads it back by the same analysis id. No SQL for the
// document — the version int and body both come straight from object storage.

/** A produced working-document version: its number and markdown body. */
export interface WorkingDocument {
  version: number;
  markdown: string;
}

// Matches the trailing `versions/<n>` segment; `<n>` is zero-padded on write.
const VERSION_NAME = /\/versions\/(\d+)$/;

// The store zero-pads each version to a fixed width so a lexical listing sorts by
// version (themis/services/store/storage.py `_VERSION_WIDTH`). A direct-version
// key must reproduce that padding to hit the written object.
const VERSION_WIDTH = 12;

export class Gcs {
  private storage?: Storage;

  constructor(private readonly config: GcsConfig) {}

  private client(): Storage {
    if (!this.storage) {
      this.storage = new Storage();
    }
    return this.storage;
  }

  private prefix(analysisId: string): string {
    return `${analysisId}/versions/`;
  }

  private versionKey(analysisId: string, version: number): string {
    return `${this.prefix(analysisId)}${String(version).padStart(VERSION_WIDTH, "0")}`;
  }

  /** The highest-numbered version, or null when none has been written yet (the
   *  loud "not produced" state the document pane must handle). */
  async latestWorkingDocument(
    analysisId: string,
  ): Promise<WorkingDocument | null> {
    const [files] = await this.client()
      .bucket(this.config.workingDocumentBucket)
      .getFiles({ prefix: this.prefix(analysisId) });
    let best: { version: number; file: File } | null = null;
    for (const file of files) {
      const match = VERSION_NAME.exec(file.name);
      if (match === null) continue;
      const version = Number(match[1]);
      if (best === null || version > best.version) {
        best = { version, file };
      }
    }
    if (best === null) return null;
    return { version: best.version, markdown: await downloadText(best.file) };
  }

  /** A specific historical version, or null when that version object is absent. */
  async workingDocumentVersion(
    analysisId: string,
    version: number,
  ): Promise<WorkingDocument | null> {
    const file = this.client()
      .bucket(this.config.workingDocumentBucket)
      .file(this.versionKey(analysisId, version));
    const [exists] = await file.exists();
    if (!exists) return null;
    return { version, markdown: await downloadText(file) };
  }
}

async function downloadText(file: File): Promise<string> {
  const [buffer] = await file.download();
  return buffer.toString("utf8");
}
