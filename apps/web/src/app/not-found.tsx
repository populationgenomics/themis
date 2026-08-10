import Link from "next/link";
import { NotFoundScene } from "@/components/not-found-scene";

export default function NotFound() {
  return (
    <main className="flex min-h-svh flex-col items-center justify-center gap-8 p-8">
      <div className="flex items-center">
        <h1 className="mr-[20px] border-line-primary border-r pr-[23px] text-2xl font-medium text-ink-primary leading-[49px]">
          404
        </h1>
        <h2 className="text-sm font-normal text-ink-body leading-[49px]">
          This page could not be found.
        </h2>
      </div>
      <NotFoundScene className="aspect-[2/1] w-full max-w-[600px]" />
      <Link href="/" className="text-sm text-ink-faint hover:text-ink-primary">
        Return to the workbench
      </Link>
    </main>
  );
}
