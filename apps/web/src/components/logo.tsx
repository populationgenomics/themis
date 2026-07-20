import Image from "next/image";
import themisLogo from "../../public/themis-logo.svg";

// The Themis mark. A static import carries the SVG's intrinsic dimensions so the
// aspect ratio is fixed and the caller sizes it by height (`h-[30px] w-auto`).
// `unoptimized` (Next applies it automatically for `.svg`) serves the vector
// as-is — no runtime image optimizer (sharp) needed.
export function Logo({ className }: { className?: string }) {
  return (
    <Image
      src={themisLogo}
      alt="Themis"
      className={className}
      priority
      unoptimized
    />
  );
}
