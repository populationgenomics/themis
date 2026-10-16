/** The worksheet-level sections' ids. They are stored exactly as workflows are, under these ids in
 *  the same table, with the same auto-save and the same submission snapshot, so nothing downstream
 *  needs a second read path for them. */
export const ROUTING_ID = "routing";
export const VERDICT_ID = "verdict";
