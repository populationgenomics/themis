import { Newsreader } from "next/font/google";
import type { ReactNode } from "react";
import "@/curation/ui/curation.css";

// The curator's own voice — the one face this surface adds. Scoped to the curation routes so the
// workbench's type stays as it is.
const curator = Newsreader({
  subsets: ["latin"],
  display: "swap",
  variable: "--font-curator-serif",
});

export default function CurationLayout({ children }: { children: ReactNode }) {
  return (
    <div className={`${curator.variable} min-h-svh bg-surface-doc-pane`}>
      {children}
    </div>
  );
}
