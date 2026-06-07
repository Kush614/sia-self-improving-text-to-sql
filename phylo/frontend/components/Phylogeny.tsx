"use client";
import { useEffect, useRef } from "react";
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { EffectComposer } from "three/examples/jsm/postprocessing/EffectComposer.js";
import { RenderPass } from "three/examples/jsm/postprocessing/RenderPass.js";
import { UnrealBloomPass } from "three/examples/jsm/postprocessing/UnrealBloomPass.js";

export type Node = {
  id: string; gen: number; pop: number; agent: number;
  operator: string; fitness: number | null;
  parent_a: string | null; parent_b: string | null; crossover_points?: string[];
  system_prompt?: string; meta_instructions?: string; output_format?: string;
  similar?: { id: string; fitness: number; score: number }[];
};
export type Lineage = { nodes: Node[]; edges: { source: string; target: string; operator: string }[];
  generations: number[]; populations: number[]; best: Node | null };

// cyberpunk neon edge colors
const OPCOLOR: Record<string, number> = {
  mutation: 0xff2bd6, crossover: 0x16f4ff, migration: 0xffe24b, elite: 0xa46bff, seed: 0xffffff,
};

export default function Phylogeny({ lineage, onSelect }: { lineage: Lineage | null; onSelect: (n: Node) => void }) {
  const mount = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!lineage || !mount.current) return;
    const el = mount.current;
    const W = () => el.clientWidth || window.innerWidth, H = () => el.clientHeight || window.innerHeight;

    const renderer = new THREE.WebGLRenderer({ antialias: true });
    renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
    renderer.setSize(W(), H());
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    el.appendChild(renderer.domElement);

    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0x05060f);
    scene.fog = new THREE.FogExp2(0x05060f, 0.0016);
    const camera = new THREE.PerspectiveCamera(58, W() / H(), 0.1, 5000);
    camera.position.set(90, 70, 165);
    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true; controls.autoRotate = true; controls.autoRotateSpeed = 0.55;
    scene.add(new THREE.AmbientLight(0x335577, 0.6));
    const p1 = new THREE.PointLight(0x16f4ff, 1.2, 0); p1.position.set(-80, 60, 80); scene.add(p1);
    const p2 = new THREE.PointLight(0xff2bd6, 1.0, 0); p2.position.set(90, -20, -60); scene.add(p2);

    // bloom (neon glow)
    const composer = new EffectComposer(renderer);
    composer.addPass(new RenderPass(scene, camera));
    const bloom = new UnrealBloomPass(new THREE.Vector2(W(), H()), 0.9, 0.7, 0.18);
    composer.addPass(bloom);
    composer.setSize(W(), H());

    // neon grid floor
    const grid = new THREE.GridHelper(900, 60, 0x16f4ff, 0x12314a);
    (grid.material as THREE.Material).transparent = true; (grid.material as THREE.Material).opacity = 0.25;
    grid.position.y = -((lineage.generations.length) * 18) / 2 - 24;
    scene.add(grid);

    // starfield
    const starGeo = new THREE.BufferGeometry();
    const starN = 900, sp = new Float32Array(starN * 3);
    for (let i = 0; i < starN; i++) { sp[i*3]=(Math.random()-0.5)*1600; sp[i*3+1]=(Math.random()-0.5)*1000; sp[i*3+2]=(Math.random()-0.5)*1600; }
    starGeo.setAttribute("position", new THREE.BufferAttribute(sp, 3));
    scene.add(new THREE.Points(starGeo, new THREE.PointsMaterial({ color: 0x2a4a6a, size: 1.4 })));

    // ── cluster layout: pop = cluster center on X, gen = height, agents orbit in cluster ──
    const gens = lineage.generations, pops = lineage.populations;
    const perPop = Math.max(...lineage.nodes.map((n) => n.agent)) + 1;
    const X = 70, Y = 18, R = 13;
    const pos: Record<string, THREE.Vector3> = {};
    for (const n of lineage.nodes) {
      const cx = (n.pop - (pops.length - 1) / 2) * X;
      const ang = (n.agent / Math.max(1, perPop)) * Math.PI * 2 + n.gen * 0.5;
      const rad = perPop > 1 ? R : 0;
      pos[n.id] = new THREE.Vector3(
        cx + Math.cos(ang) * rad,
        (n.gen - (gens[0] || 1)) * Y - (gens.length - 1) * Y / 2,
        Math.sin(ang) * rad,
      );
    }
    // fitness color: low magenta -> high cyan-green (neon)
    const fs = lineage.nodes.map((n) => n.fitness ?? 0);
    const fmin = Math.min(...fs), fmax = Math.max(...fs);
    const col = (f: number | null) => {
      const t = fmax > fmin ? (((f ?? 0) - fmin) / (fmax - fmin)) : 0.5;
      return new THREE.Color().setHSL(0.83 - t * 0.38, 1.0, 0.55); // 0.83(magenta)->0.45(cyan/green)
    };

    // edges (additive neon)
    for (const e of lineage.edges) {
      const a = pos[e.source], b = pos[e.target]; if (!a || !b) continue;
      const g = new THREE.BufferGeometry().setFromPoints([a, b]);
      scene.add(new THREE.Line(g, new THREE.LineBasicMaterial({
        color: OPCOLOR[e.operator] || 0x8a8a99, transparent: true, opacity: 0.55, blending: THREE.AdditiveBlending,
      })));
    }

    // nodes (emissive -> glow via bloom) + halo sprites
    const geo = new THREE.SphereGeometry(2.4, 24, 24);
    const meshes: THREE.Mesh[] = [];
    const bestId = lineage.best?.id;
    const haloTex = makeHalo();
    lineage.nodes.forEach((n, i) => {
      const c = col(n.fitness);
      const tnorm = fmax > fmin ? (((n.fitness ?? 0) - fmin) / (fmax - fmin)) : 0.5;
      const mesh = new THREE.Mesh(geo, new THREE.MeshStandardMaterial({ color: c, emissive: c, emissiveIntensity: 1.0, roughness: 0.3, metalness: 0.2 }));
      mesh.position.copy(pos[n.id]); mesh.scale.setScalar(0.01);
      const isBest = n.id === bestId;
      const halo = new THREE.Sprite(new THREE.SpriteMaterial({ map: haloTex, color: c, transparent: true, opacity: 0.5, blending: THREE.AdditiveBlending, depthWrite: false }));
      halo.scale.setScalar(isBest ? 22 : 12); mesh.add(halo);
      Object.assign(mesh as any, {
        node: n, best: isBest, halo,
        phase: i * 0.7,                       // twinkle offset
        glowBase: 0.5 + tnorm * 1.4,          // fitter agents glow brighter ("accordingly")
        glowAmp: 0.35 + tnorm * 0.5,          // and pulse stronger
      });
      scene.add(mesh); meshes.push(mesh);
    });
    // gen-by-gen spawn-in
    gens.forEach((g, i) => setTimeout(() => {
      meshes.filter((m) => (m as any).node.gen === g).forEach((m) => {
        const target = (m as any).best ? 2.1 : 1.0; let s = 0.01;
        const grow = () => { s += (target - s) * 0.18; m.scale.setScalar(s); if (Math.abs(target - s) > 0.02) requestAnimationFrame(grow); else { m.scale.setScalar(target); (m as any).grown = true; } };
        grow();
      });
    }, i * 600));

    // anime billboards (drop PNGs in /public/anime/a1.png, a2.png … — gracefully skipped if absent)
    const loader = new THREE.TextureLoader();
    pops.forEach((popIdx, i) => {
      loader.load(`/anime/a${i + 1}.png`, (tex) => {
        const spr = new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, transparent: true, opacity: 0.96 }));
        const cx = (popIdx - (pops.length - 1) / 2) * X;
        spr.position.set(cx, (gens.length * Y) / 2 + 26, 0); spr.scale.set(34, 34, 1);
        scene.add(spr);
      }, undefined, () => {});
    });

    const ray = new THREE.Raycaster(), mouse = new THREE.Vector2();
    const onClick = (ev: MouseEvent) => {
      const r = renderer.domElement.getBoundingClientRect();
      mouse.x = ((ev.clientX - r.left) / r.width) * 2 - 1;
      mouse.y = -((ev.clientY - r.top) / r.height) * 2 + 1;
      ray.setFromCamera(mouse, camera);
      const hit = ray.intersectObjects(meshes)[0];
      if (hit) { controls.autoRotate = false; onSelect((hit.object as any).node as Node); }
    };
    renderer.domElement.addEventListener("click", onClick);

    let raf = 0, t = 0;
    const loop = () => {
      raf = requestAnimationFrame(loop); t += 0.03; controls.update();
      meshes.forEach((m) => {
        const a = m as any;
        // glow on/off: breathing emissive pulse, brighter for fitter agents
        const pulse = a.glowBase + Math.sin(t * 1.7 + a.phase) * a.glowAmp;
        (m.material as THREE.MeshStandardMaterial).emissiveIntensity = Math.max(0.1, pulse);
        if (a.halo) (a.halo.material as THREE.SpriteMaterial).opacity = 0.3 + Math.max(0, Math.sin(t * 1.7 + a.phase)) * 0.5;
        if (a.best && a.grown) m.scale.setScalar(2.1 + Math.sin(t) * 0.18);
      });
      composer.render();
    };
    loop();
    const onResize = () => { renderer.setSize(W(), H()); composer.setSize(W(), H()); camera.aspect = W() / H(); camera.updateProjectionMatrix(); };
    window.addEventListener("resize", onResize);

    return () => {
      cancelAnimationFrame(raf); window.removeEventListener("resize", onResize);
      renderer.domElement.removeEventListener("click", onClick);
      composer.dispose(); renderer.dispose();
      if (renderer.domElement.parentNode === el) el.removeChild(renderer.domElement);
    };
  }, [lineage, onSelect]);

  return <div ref={mount} style={{ position: "absolute", inset: 0 }} />;
}

// radial-gradient halo texture for the neon glow sprites
function makeHalo(): THREE.Texture {
  const c = document.createElement("canvas"); c.width = c.height = 128;
  const ctx = c.getContext("2d")!;
  const g = ctx.createRadialGradient(64, 64, 0, 64, 64, 64);
  g.addColorStop(0, "rgba(255,255,255,1)"); g.addColorStop(0.25, "rgba(255,255,255,0.5)"); g.addColorStop(1, "rgba(255,255,255,0)");
  ctx.fillStyle = g; ctx.fillRect(0, 0, 128, 128);
  const tex = new THREE.Texture(c); tex.needsUpdate = true; return tex;
}
