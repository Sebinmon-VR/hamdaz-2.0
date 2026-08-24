import type { Metadata, Viewport } from "next";
import { Archivo, JetBrains_Mono } from "next/font/google";
import type { ReactNode } from "react";

import "./globals.css";

/**
 * Fonts are self-hosted by Next rather than fetched from Google at runtime: no third-party
 * request on every page load, and no flash of fallback text. The legacy app pulled three
 * stylesheets from two CDNs before it rendered anything.
 */
const archivo = Archivo({
  subsets: ["latin"],
  weight: ["400", "500", "600", "700"],
  variable: "--font-archivo",
  display: "swap",
});

const jetbrainsMono = JetBrains_Mono({
  subsets: ["latin"],
  weight: ["400", "500", "700"],
  variable: "--font-jetbrains",
  display: "swap",
});

export const metadata: Metadata = {
  title: {
    default: "Hamdaz",
    template: "%s · Hamdaz",
  },
  description: "Hamdaz 2.0 — multi-team operations platform.",
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#f4f6f5" },
    { media: "(prefers-color-scheme: dark)", color: "#0e1519" },
  ],
};

export default function RootLayout({ children }: { children: ReactNode }): ReactNode {
  return (
    <html lang="en" className={`${archivo.variable} ${jetbrainsMono.variable}`}>
      <body>{children}</body>
    </html>
  );
}
