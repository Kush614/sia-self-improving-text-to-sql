"use client";
import { useEffect, useMemo, useState, useCallback } from "react";
import { CopilotSidebar } from "@copilotkit/react-ui";
import { useCopilotReadable, useCopilotAction } from "@copilotkit/react-core";
import Phylogeny, { Lineage, Node } from "@/components/Phylogeny";
import HarnessEditor from "@/components/HarnessEditor";

const OPC: Record<string, string> = {
  mutation: "#ff2bd6", crossover: "#16f4ff", migration: "#ffe24b", elite: "#a46bff", seed: "#ffffff",
};
const pct = (f: number | null | undefined) => (f == null ? "—" : (f * 100).toFixed(1) + "%");

export default function Home() {
  const [lineage, setLineage] = useState<Lineage | null>(null);
  const [selected, setSelected] = useState<Node | null>(null);
  const [live, setLive] = useState<{ n: number; on: boolean; items: { id: string; op: string }[] }>({ n: 0, on: false, items: [] });

  useEffect(() => {
    fetch("/lineage.json").then((r) => r.json()).then(setLineage).catch(() => {});
  }, []);

  // live Redis pub/sub -> SSE -> frontend: new-agent events stream in as evolution runs
  useEffect(() => {
    const es = new EventSource("/api/stream");
    es.onmessage = (ev) => {
      let m: any; try { m = JSON.parse(ev.data); } catch { return; }
      if (m.type === "agent")
        setLive((s) => ({ n: s.n + 1, on: true, items: [{ id: m.id, op: "spawned " + m.operator }, ...s.items].slice(0, 7) }));
      else if (m.type === "fitness")
        setLive((s) => ({ n: s.n + 1, on: true, items: [{ id: m.id, op: "scored " + Math.round((m.fitness || 0) * 100) + "%" }, ...s.items].slice(0, 7) }));
    };
    return () => es.close();
  }, []);

  const onSelect = useCallback((n: Node) => setSelected(n), []);
  const byId = useMemo(() => Object.fromEntries((lineage?.nodes || []).map((n) => [n.id, n])), [lineage]);

  // ── expose the phylogeny + selection to the copilot ───────────────────────
  useCopilotReadable({
    description: "Phylogeny of evolved text-to-SQL agent harnesses: per-node fitness (execution accuracy), operator (seed/mutation/crossover/migration/elite), parents, and the prompt sections. The model is fixed; only the harness evolves.",
    value: JSON.stringify(lineage ? {
      generations: lineage.generations, populations: lineage.populations,
      best: lineage.best && { id: lineage.best.id, fitness: lineage.best.fitness },
      nodes: lineage.nodes.map((n) => ({ id: n.id, gen: n.gen, pop: n.pop, operator: n.operator,
        fitness: n.fitness, parent_a: n.parent_a, parent_b: n.parent_b, crossover_points: n.crossover_points })),
    } : "loading"),
  });
  useCopilotReadable({ description: "The currently selected agent node, full genome", value: selected ? JSON.stringify(selected) : "none" });

  // ── generative ancestor diff (CopilotKit generative UI) ───────────────────
  useCopilotAction({
    name: "explainAncestorDiff",
    description: "Explain how an agent differs from its parent(s): which genome sections came from which parent (crossover), what a mutation changed, and how fitness compares. Use the readable phylogeny data.",
    parameters: [{ name: "agentId", type: "string", description: "e.g. gen_3:pop_1:agent_2", required: true }],
    handler: async ({ agentId }: { agentId: string }) => {
      const n = byId[agentId]; if (!n) return `No agent ${agentId}`;
      const parents = [n.parent_a, n.parent_b].filter(Boolean).map((p) => byId[p as string]).filter(Boolean);
      return JSON.stringify({
        child: { id: n.id, operator: n.operator, fitness: n.fitness, crossover_points: n.crossover_points,
          sections: { system_prompt: n.system_prompt, meta_instructions: n.meta_instructions, output_format: n.output_format } },
        parents: parents.map((p) => ({ id: p.id, fitness: p.fitness,
          sections: { system_prompt: p.system_prompt, meta_instructions: p.meta_instructions, output_format: p.output_format } })),
      });
    },
    render: ({ status, args }: any) => (
      <div className="diffcard">
        <h4>🧬 ancestor diff — {args?.agentId}</h4>
        <div style={{ color: "#6f86a8" }}>{status === "complete" ? "see explanation in chat" : "analyzing lineage…"}</div>
      </div>
    ),
  });

  // generative UI: open an interactive live editor of a genome section IN the chat
  useCopilotAction({
    name: "openHarnessEditor",
    description: "Open an interactive live code editor in the chat showing a genome section (system_prompt, meta_instructions, or output_format) of an agent harness so the user can view and tweak the evolved prompt. Defaults to the best agent + system_prompt.",
    parameters: [
      { name: "agentId", type: "string", description: "agent id like gen_4:pop_0:agent_1; omit for the best agent", required: false },
      { name: "section", type: "string", description: "system_prompt | meta_instructions | output_format", required: false },
    ],
    handler: async ({ agentId, section }: { agentId?: string; section?: string }) => {
      const n = (agentId && byId[agentId]) || (lineage?.best ? byId[lineage.best.id] : null);
      const sec = (section as keyof Node) || "system_prompt";
      return n ? String((n as any)[sec] ?? "") : "no agent found";
    },
    render: ({ args, result, status }: any) => {
      const n = (args?.agentId && byId[args.agentId]) || (lineage?.best ? byId[lineage.best.id] : null);
      const sec = args?.section || "system_prompt";
      const code = n ? String((n as any)[sec] ?? "") : (typeof result === "string" ? result : "");
      return <HarnessEditor title={`${n?.id || "?"} · ${sec}`} code={code || (status !== "complete" ? "loading…" : "")} />;
    },
  });

  const best = lineage?.best;
  const strong = (lineage?.nodes || []).filter((n) => (n.fitness || 0) >= 0.85).length;

  return (
    <main>
      <Phylogeny lineage={lineage} onSelect={onSelect} />

      <div className="hud" id="title">
        <h1>Phylo · evolutionary search over agent harnesses</h1>
        <p>Each node is an agent harness. Height = generation, columns = populations, color = fitness,
          edges = lineage. Traced in <a href="https://wandb.ai/kushise27-kush/phylo/weave" target="_blank">Weave</a>.
          Ask the copilot → to explain any agent&apos;s ancestry.</p>
      </div>

      {lineage && (
        <div className="hud stats glass">
          <div className="k">best agent</div>
          <div className="big">{pct(best?.fitness)}</div>
          <div className="k">{best?.id}</div>
          <div className="k" style={{ marginTop: 6 }}>{lineage.nodes.length} agents · {lineage.generations.length} gens · {lineage.populations.length} pops · ≥85%: {strong}</div>
        </div>
      )}

      {live.n > 0 && (
        <div className="hud live glass">
          <div className="hdr"><span className="dot">●</span> LIVE · Redis pub/sub <span style={{ color: "var(--muted)" }}>({live.n})</span></div>
          {live.items.map((it, i) => (
            <div key={i} className="row" style={{ opacity: 1 - i * 0.11 }}>+ {it.id} <span style={{ color: "#7fb0d8" }}>{it.op}</span></div>
          ))}
        </div>
      )}

      <div className="hud mascot">
        <span className="face">🌸</span>
        <div className="tag">PHYLO-CHAN<br /><b>gen {lineage?.generations.length ?? 0}</b> · evolving</div>
      </div>

      <div className="hud legend glass">
        <div><span className="sw" style={{ background: "#ff5d5d" }} />low <span className="sw" style={{ background: "#ffc900", marginLeft: 8 }} />mid <span className="sw" style={{ background: "#23e0b0", marginLeft: 8 }} />high fitness</div>
        <div><b>edges:</b> <span className="sw" style={{ background: OPC.mutation }} />mutation <span className="sw" style={{ background: OPC.crossover, marginLeft: 8 }} />crossover <span className="sw" style={{ background: OPC.migration, marginLeft: 8 }} />migration <span className="sw" style={{ background: OPC.elite, marginLeft: 8 }} />elite</div>
      </div>

      {selected && (
        <div className="hud panel glass">
          <span className="close" onClick={() => setSelected(null)}>✕</span>
          <h3>{selected.id}</h3>
          <span className="tag" style={{ background: OPC[selected.operator] || "#8a8a99" }}>{selected.operator}</span>
          <span className="tag" style={{ background: "#23e0b0" }}>fitness {pct(selected.fitness)}</span>
          <div><span className="k">gen</span> {selected.gen} · <span className="k">pop</span> {selected.pop} · <span className="k">agent</span> {selected.agent}</div>
          <div><span className="k">parents:</span> {[selected.parent_a, selected.parent_b].filter(Boolean).join(", ") || "—"}</div>
          <div style={{ marginTop: 4 }}><a href="https://wandb.ai/kushise27-kush/phylo/weave" target="_blank" rel="noreferrer">🔭 view this agent&apos;s traces in Weave</a></div>
          {selected.crossover_points && selected.crossover_points.length > 0 && (
            <div><span className="k">sections from parent B:</span> {selected.crossover_points.join(", ")}</div>
          )}
          {selected.similar && selected.similar.length > 0 && (
            <div style={{ marginTop: 6 }}>
              <span className="k">similar harnesses (Redis vector search):</span>
              <div>{selected.similar.map((s) => (
                <div key={s.id} style={{ fontSize: 12 }}>{s.id} · sim {s.score} · {pct(s.fitness)}</div>
              ))}</div>
            </div>
          )}
          <div className="k" style={{ marginTop: 8 }}>system_prompt</div><pre>{selected.system_prompt}</pre>
          <div className="k">meta_instructions</div><pre>{selected.meta_instructions}</pre>
          <div className="k">output_format</div><pre>{selected.output_format}</pre>
        </div>
      )}

      <CopilotSidebar
        defaultOpen={false}
        labels={{ title: "Phylo copilot", initial: "Ask me about any agent's ancestry, e.g. \"explain the ancestor diff for the best agent\" or \"why did population 1 improve?\"" }}
      />
    </main>
  );
}
