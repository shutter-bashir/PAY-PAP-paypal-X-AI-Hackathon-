import type { Metadata } from "next";
import { Inter } from "next/font/google";
import { Suspense } from "react";
import "./globals.css";
import AppShell from "@/components/AppShell";

// iOS look: San Francisco on Apple devices (system font); Inter, its closest match, elsewhere.
const inter = Inter({ variable: "--font-inter", subsets: ["latin"] });

export const metadata: Metadata = {
  title: "PAY PAP",
  description: "AI-checked bulk contractor payouts through PayPal, released only after a human approves.",
};

// Sets light/dark before the first paint so the page never flashes the wrong theme.
const themeScript = `try{var t=localStorage.getItem("paypap.theme");if(t!=="light"&&t!=="dark"){t=matchMedia("(prefers-color-scheme: dark)").matches?"dark":"light"}document.documentElement.dataset.theme=t}catch(e){}`;

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className={inter.variable} suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: themeScript }} />
      </head>
      <body>
        {/* Every page reads the URL and the browser session, so the app renders on the client. */}
        <Suspense fallback={null}>
          <AppShell>{children}</AppShell>
        </Suspense>
      </body>
    </html>
  );
}
