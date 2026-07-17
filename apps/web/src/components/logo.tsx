import Image from "next/image";
import themisLogo from "../../public/themis-logo.png";

// The Themis mark. The real asset ships in `public/`; a static
// import carries its intrinsic dimensions so the aspect ratio is fixed and the
// caller sizes it by height (`h-[30px] w-auto`). `unoptimized` serves the PNG
// as-is — no runtime image optimizer (sharp) needed for a tiny logo.
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
