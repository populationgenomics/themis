// Next writes this same reference into next-env.d.ts, which is gitignored and only
// produced by `next build`; `tsc --noEmit` runs standalone, so the static-image
// module declarations (`*.png` -> StaticImageData) must be committed to resolve.
/// <reference types="next/image-types/global" />
