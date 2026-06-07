import { CopilotRuntime, OpenAIAdapter, copilotRuntimeNextJSAppRouterEndpoint } from "@copilotkit/runtime";
import { NextRequest } from "next/server";

// OpenAIAdapter reads OPENAI_API_KEY from env. Model used for the in-app copilot.
const serviceAdapter = new OpenAIAdapter({ model: process.env.COPILOT_MODEL || "gpt-4o" });
const runtime = new CopilotRuntime();

export const POST = async (req: NextRequest) => {
  const { handleRequest } = copilotRuntimeNextJSAppRouterEndpoint({
    runtime,
    serviceAdapter,
    endpoint: "/api/copilotkit",
  });
  return handleRequest(req);
};
