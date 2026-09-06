import React, { useEffect, useRef } from 'react';

export const AnimatedNetwork: React.FC = () => {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    let animationFrameId: number;
    let width = (canvas.width = canvas.parentElement?.clientWidth || 800);
    let height = (canvas.height = canvas.parentElement?.clientHeight || 450);

    const handleResize = () => {
      if (!canvas || !canvas.parentElement) return;
      width = canvas.width = canvas.parentElement.clientWidth;
      height = canvas.height = canvas.parentElement.clientHeight;
    };
    window.addEventListener('resize', handleResize);

    const nodes = Array.from({ length: 32 }, (_, i) => ({
      x: Math.random() * width,
      y: Math.random() * height,
      vx: (Math.random() - 0.5) * 0.6,
      vy: (Math.random() - 0.5) * 0.6,
      radius: Math.random() * 2.5 + 1.5,
      color: i % 4 === 0 ? '#6366f1' : i % 4 === 1 ? '#8b5cf6' : i % 4 === 2 ? '#06b6d4' : '#10b981',
      label: i === 0 ? 'Qdrant Vector' : i === 1 ? 'LangGraph Agent' : i === 2 ? 'MCP Connector' : undefined,
    }));

    const draw = () => {
      ctx.clearRect(0, 0, width, height);

      // Connect nodes
      for (let i = 0; i < nodes.length; i++) {
        for (let j = i + 1; j < nodes.length; j++) {
          const dx = nodes[i].x - nodes[j].x;
          const dy = nodes[i].y - nodes[j].y;
          const dist = Math.sqrt(dx * dx + dy * dy);

          if (dist < 130) {
            const alpha = 1 - dist / 130;
            ctx.beginPath();
            ctx.moveTo(nodes[i].x, nodes[i].y);
            ctx.lineTo(nodes[j].x, nodes[j].y);
            ctx.strokeStyle = `rgba(99, 102, 241, ${alpha * 0.25})`;
            ctx.lineWidth = 1;
            ctx.stroke();
          }
        }
      }

      // Render nodes
      nodes.forEach((n) => {
        n.x += n.vx;
        n.y += n.vy;

        if (n.x < 0 || n.x > width) n.vx *= -1;
        if (n.y < 0 || n.y > height) n.vy *= -1;

        ctx.beginPath();
        ctx.arc(n.x, n.y, n.radius, 0, Math.PI * 2);
        ctx.fillStyle = n.color;
        ctx.shadowColor = n.color;
        ctx.shadowBlur = 10;
        ctx.fill();

        if (n.label) {
          ctx.font = '10px JetBrains Mono, monospace';
          ctx.fillStyle = 'rgba(226, 232, 240, 0.7)';
          ctx.fillText(n.label, n.x + 8, n.y + 3);
        }
      });

      animationFrameId = requestAnimationFrame(draw);
    };

    draw();

    return () => {
      window.removeEventListener('resize', handleResize);
      cancelAnimationFrame(animationFrameId);
    };
  }, []);

  return (
    <div className="w-full h-full relative overflow-hidden rounded-2xl border border-slate-800/80 bg-slate-950/80 backdrop-blur-xl">
      <canvas ref={canvasRef} className="w-full h-full block" />
      <div className="absolute top-4 left-4 flex items-center gap-2 px-3 py-1.5 rounded-full bg-slate-900/90 border border-slate-800 text-[11px] font-mono text-slate-300">
        <span className="h-2 w-2 rounded-full bg-emerald-400 animate-ping" />
        <span>RAG Pipeline Live Graph</span>
      </div>
    </div>
  );
};
