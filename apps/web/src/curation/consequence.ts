import { Consequence } from "@/gen/themis/evidence/models/evidence_pb";

/** Every class the contract names, in the order the calculator groups them. Non-coding is here even
 *  though no predicted-effect workflow is transcribed for it: a curator whose variant is non-coding
 *  has to be able to say so, and an empty routed set is the framework's answer to that. */
export const CONSEQUENCES: [Consequence, string][] = [
  [Consequence.MISSENSE, "Missense"],
  [Consequence.NONSENSE, "Nonsense"],
  [Consequence.FRAMESHIFT, "Frameshift"],
  [Consequence.INFRAME_INDEL, "In-frame indel"],
  [Consequence.CANONICAL_SPLICE, "Canonical splice"],
  [Consequence.INTRONIC, "Intronic"],
  [Consequence.SYNONYMOUS, "Synonymous"],
  [Consequence.NON_CODING, "Non-coding"],
  [Consequence.EXON_DELETION, "Exon deletion"],
  [Consequence.EXON_DUPLICATION, "Exon duplication"],
  [Consequence.START_LOST, "Start lost"],
  [Consequence.STOP_LOST, "Stop lost"],
];

/** The class as the pickers name it; "not stated" for a routing that names none yet. */
export function consequenceLabel(consequence: Consequence): string {
  if (consequence === Consequence.UNSPECIFIED) return "not stated";
  const found = CONSEQUENCES.find(([value]) => value === consequence);
  if (found === undefined) {
    throw new Error(
      `${consequence} is not a consequence class the worksheet offers`,
    );
  }
  return found[1];
}
