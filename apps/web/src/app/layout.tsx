import type { Metadata } from "next";
import { Providers } from "./providers";
import "./globals.css";
import { Familjen_Grotesk, Spline_Sans_Mono } from "next/font/google";
import { cn } from "@/lib/utils";

// Both are variable fonts; the wght axis covers the design's used weights
// (Familjen Grotesk 400–700, Spline Sans Mono 400–600), so no static `weight`
// list is needed. Mono is load-bearing: every identifier/eyebrow renders in it.
const familjenGrotesk = Familjen_Grotesk({
  subsets: ["latin"],
  variable: "--font-sans",
});

const splineSansMono = Spline_Sans_Mono({
  subsets: ["latin"],
  variable: "--font-mono",
});

export const metadata: Metadata = {
  title: "Themis",
  description: "Themis variant curation workbench",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    // suppressHydrationWarning: browser extensions inject attributes (e.g. data-spf-installed) onto
    // html/body before hydration; the diff is theirs, not ours, and would otherwise error under React.
    <html
      lang="en"
      suppressHydrationWarning
      className={cn(
        "font-sans",
        familjenGrotesk.variable,
        splineSansMono.variable,
      )}
    >
      <body
        suppressHydrationWarning
        className="min-h-svh bg-background font-sans text-ink-body antialiased"
      >
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
