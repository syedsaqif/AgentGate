import { useEffect, useRef, type CSSProperties } from "react";
import { pipeline } from "../demo/gateTimeline";
import "./walkthrough.css";

export function PaymentPath() {
  return <svg className="payment-path" viewBox="0 0 340 112" aria-hidden="true">
    <path className="path-rail" d="M18 56H322" />
    <path className="path-ink" pathLength="1" d="M18 56H322" />
    {pipeline.map(([key], index) => <circle className="path-check" key={key}
      cx={70 + index * 22} cy="56" r="3.5" style={{ "--delay": (52 + index * 22) / 304 * 1.9 + "s" } as CSSProperties} />)}
    <g className="path-packet"><circle cx="18" cy="56" r="11" opacity=".16" /><circle cx="18" cy="56" r="4.8" /></g>
    <text x="8" y="32">BillPal</text><text x="169" y="88" textAnchor="middle">10 policy stages</text>
    <text x="332" y="32" textAnchor="end">Ledger</text>
  </svg>;
}

export function MandateScale() {
  return <div className="mandate-scale">
    <div className="mandate-track"><i /></div>
    <span>Confirm above ₹4,000</span>
  </div>;
}

const steps = [
  ["Set up a mandate", "Sign in to Demo Bank with PIN 4821 to adjust BillPal’s spending limits."],
  ["Run a scenario", "In Presenter Stage, choose a normal payment or a security test and watch the checks unfold."],
  ["Verify a receipt", "Download an executed payment’s QR code to verify its receipt offline."],
];

function Illustration({ index }: { index: number }) {
  if (index === 0) return <><span>BillPal mandate</span><strong>₹5,000</strong><small>Per-payment limit</small><MandateScale /></>;
  if (index === 1) return <><span>Payment journey</span><strong>₹1,850</strong><small>Electricity Board</small><PaymentPath /></>;
  return <><span>Bank ledger</span><div className="story-receipt"><small>Payment receipt</small><strong>₹1,850</strong><small>Electricity Board</small><div className="receipt-status">✓ Executed</div></div></>;
}

export function Walkthrough() {
  const section = useRef<HTMLElement>(null);
  useEffect(() => {
    const root = section.current;
    if (!root) return;
    const rows = Array.from(root.querySelectorAll<HTMLElement>(".story-step"));
    const scenes = root.querySelectorAll<HTMLElement>(".story-stage .story-art");
    let frame = 0;
    function paint() {
      frame = 0;
      const bounds = rows.map(row => row.getBoundingClientRect());
      const center = window.innerHeight * .5;
      bounds.forEach((rect, index) => {
        const start = rect.top + rect.height * .5;
        const next = bounds[index + 1];
        const interval = next ? next.top + next.height * .5 - start : rect.height;
        const progress = Math.max(0, Math.min(1, (center - start) / Math.max(1, interval)));
        const previous = bounds[index - 1];
        const entry = previous ? Math.min(1, Math.max(0, (center - (previous.top + previous.height * .5)) / (start - previous.top - previous.height * .5))) : 1;
        const opacity = Math.min(entry, next ? 1 - progress : 1);
        scenes[index].style.setProperty("--presence", String(opacity));
        const reveal = Math.min(1, Math.max(0, (window.innerHeight - rect.top) / Math.max(1, rect.height * .65)));
        rows[index].style.setProperty("--reveal", String(reveal));
      });
    }
    function schedule() { if (!frame) frame = requestAnimationFrame(paint); }
    paint();
    window.addEventListener("scroll", schedule, { passive: true });
    window.addEventListener("resize", schedule);
    return () => {
      window.removeEventListener("scroll", schedule);
      window.removeEventListener("resize", schedule);
      cancelAnimationFrame(frame);
    };
  }, []);
  return <section ref={section} className="home-guide home-story" aria-labelledby="home-guide-title">
    <h2 id="home-guide-title">How to Explore</h2>
    <div className="story-layout">
      <ol>{steps.map(([title, copy], index) => <li key={title} className="story-step">
        <strong>{title}</strong><p>{copy}</p>
        <div className="story-art story-inline"><Illustration index={index} /></div>
      </li>)}</ol>
      <div className="story-stage" aria-hidden="true">
        {steps.map(([title], index) => <div key={title} className="story-art" style={{ "--presence": index === 0 ? 1 : 0 } as CSSProperties}><Illustration index={index} /></div>)}
      </div>
    </div>
  </section>;
}
