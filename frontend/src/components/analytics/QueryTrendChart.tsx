import React, { useMemo, useState } from 'react';

export interface TrendPoint {
  date: string;
  queryCount: number;
  avgLatencyMs: number;
}

/**
 * Query-volume trend. Hand-rolled SVG rather than a charting library: this is
 * the only chart in the app and a library would add more to the bundle than
 * the whole analytics route.
 */
export const QueryTrendChart: React.FC<{ data: TrendPoint[]; height?: number }> = ({
  data,
  height = 180,
}) => {
  const [hover, setHover] = useState<number | null>(null);

  const { points, areaPath, linePath, max } = useMemo(() => {
    if (data.length === 0) {
      return { points: [], areaPath: '', linePath: '', max: 0 };
    }
    const maxValue = Math.max(1, ...data.map((d) => d.queryCount));
    const stepX = data.length > 1 ? 100 / (data.length - 1) : 0;
    const pts = data.map((d, i) => ({
      x: data.length > 1 ? i * stepX : 50,
      y: 100 - (d.queryCount / maxValue) * 100,
      ...d,
    }));
    const line = pts.map((p, i) => `${i === 0 ? 'M' : 'L'} ${p.x} ${p.y}`).join(' ');
    const area = `${line} L 100 100 L 0 100 Z`;
    return { points: pts, areaPath: area, linePath: line, max: maxValue };
  }, [data]);

  if (data.length === 0) {
    return (
      <p className="py-8 text-center text-xs text-slate-500">No query activity recorded yet.</p>
    );
  }

  const active = hover !== null ? points[hover] : null;

  return (
    <figure className="w-full">
      <figcaption className="sr-only">
        Daily query volume over the last {data.length} days, peaking at {max} queries.
      </figcaption>

      <div className="relative" style={{ height }}>
        <svg
          viewBox="0 0 100 100"
          preserveAspectRatio="none"
          className="h-full w-full overflow-visible"
          role="img"
          aria-label={`Query volume trend, peak ${max}`}
        >
          <defs>
            <linearGradient id="queryTrendFill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="rgb(99 102 241)" stopOpacity="0.35" />
              <stop offset="100%" stopColor="rgb(99 102 241)" stopOpacity="0" />
            </linearGradient>
          </defs>

          {[0, 25, 50, 75, 100].map((y) => (
            <line
              key={y}
              x1="0"
              x2="100"
              y1={y}
              y2={y}
              stroke="rgb(30 41 59)"
              strokeWidth="0.4"
              vectorEffect="non-scaling-stroke"
            />
          ))}

          <path d={areaPath} fill="url(#queryTrendFill)" />
          <path
            d={linePath}
            fill="none"
            stroke="rgb(129 140 248)"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
            vectorEffect="non-scaling-stroke"
          />

          {points.map((p, i) => (
            <circle
              key={p.date}
              cx={p.x}
              cy={p.y}
              r={hover === i ? 3 : 2}
              fill={hover === i ? 'rgb(165 180 252)' : 'rgb(99 102 241)'}
              vectorEffect="non-scaling-stroke"
            />
          ))}
        </svg>

        {/* Hover targets sit above the SVG so pointer areas stay rectangular. */}
        <div className="absolute inset-0 flex">
          {points.map((p, i) => (
            <button
              key={p.date}
              type="button"
              className="h-full flex-1 cursor-default"
              onMouseEnter={() => setHover(i)}
              onMouseLeave={() => setHover(null)}
              onFocus={() => setHover(i)}
              onBlur={() => setHover(null)}
              aria-label={`${p.date}: ${p.queryCount} queries, ${p.avgLatencyMs} ms average`}
            />
          ))}
        </div>

        {active && (
          <div
            className="pointer-events-none absolute -translate-x-1/2 rounded-lg border border-slate-700 bg-slate-900 px-2.5 py-1.5 text-[10px] shadow-xl"
            style={{ left: `${active.x}%`, top: 0 }}
          >
            <p className="font-medium text-slate-200">{active.date}</p>
            <p className="text-brand-300">{active.queryCount} queries</p>
            <p className="text-slate-500">{active.avgLatencyMs} ms avg</p>
          </div>
        )}
      </div>

      <div className="mt-2 flex justify-between text-[10px] text-slate-600">
        <span>{data[0]?.date}</span>
        <span>{data[data.length - 1]?.date}</span>
      </div>
    </figure>
  );
};
