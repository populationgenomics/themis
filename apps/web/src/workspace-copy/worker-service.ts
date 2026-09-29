import { checkUserEdit } from "@/widgets/user-edit";
import { CopyService, type CopyServiceOptions } from "./service";

// The copy service as the SharedWorker runs it: every edit a publish carries is held to changing a
// user's judgements in a widget asset alone.

/** The SharedWorker's copy service over `options`. */
export function workerCopyService(
  options: Omit<CopyServiceOptions, "checkEdit">,
): CopyService {
  return new CopyService({ ...options, checkEdit: checkUserEdit });
}
