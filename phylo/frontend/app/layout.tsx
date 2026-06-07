import type { Metadata } from "next";
import { CopilotKit } from "@copilotkit/react-core";
import "@copilotkit/react-ui/styles.css";
import "./globals.css";

export const metadata: Metadata = {
  title: "Phylo — evolutionary search over agent harnesses",
  description: "A population of self-improving text-to-SQL agents evolved with selection, crossover, mutation. Traced in Weave.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>
        <CopilotKit runtimeUrl="/api/copilotkit">{children}</CopilotKit>
      </body>
    </html>
  );
}
