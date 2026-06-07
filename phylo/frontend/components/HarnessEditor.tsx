"use client";
import { useState } from "react";

// Interactive live editor rendered INSIDE the CopilotKit chat (generative UI).
export default function HarnessEditor({ title, code }: { title: string; code: string }) {
  const [text, setText] = useState(code);
  const [copied, setCopied] = useState(false);
  return (
    <div className="diffcard">
      <h4>✎ live harness editor — {title}</h4>
      <textarea
        value={text}
        spellCheck={false}
        onChange={(e) => setText(e.target.value)}
        style={{
          width: "100%", minHeight: 150, resize: "vertical",
          background: "#070b16", color: "#bfe9ff", border: "1px solid rgba(22,244,255,.35)",
          borderRadius: 8, padding: 10, fontFamily: "ui-monospace, Consolas, monospace", fontSize: 12, lineHeight: 1.5,
        }}
      />
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginTop: 6 }}>
        <span style={{ fontSize: 11, color: "#6f86a8" }}>{text.length} chars · edit freely (cached demo)</span>
        <button
          onClick={() => { navigator.clipboard?.writeText(text); setCopied(true); setTimeout(() => setCopied(false), 1200); }}
          style={{ fontSize: 11, fontWeight: 800, cursor: "pointer", color: "#05060f", background: "#16f4ff",
            border: "none", borderRadius: 6, padding: "3px 10px", boxShadow: "0 0 10px rgba(22,244,255,.6)" }}
        >{copied ? "copied ✓" : "copy"}</button>
      </div>
    </div>
  );
}
