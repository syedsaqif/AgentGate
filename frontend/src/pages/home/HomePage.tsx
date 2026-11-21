import { useEffect, useRef, useState, type CSSProperties, type KeyboardEvent } from "react";
import { CONTRACT_VERSION } from "../../api/types";
import { pipeline } from "../demo/gateTimeline";
import "./home.css";
import { MandateScale, PaymentPath, Walkthrough } from "./Walkthrough";

const destinations = [
  { name: "Presenter Stage", route: "/demo", title: "See how an AI payment gets approved.", description: "Follow a bill from BillPal’s first request through ten policy checks to its entry in the bank ledger.", preview: "Payment journey" },
  { name: "Demo Bank", route: "/demo/bank", title: "Set BillPal’s spending limits.", description: "Choose who BillPal can pay, set a limit for each payment, and decide when you need to confirm.", preview: "Agent mandate" },
  { name: "Payment Assistant", route: "/demo/agent", title: "Ask BillPal to pay a bill.", description: "Start with a payment request in your own words and follow the details BillPal sends to the gate.", preview: "BillPal conversation" },
  { name: "Payment Gate", route: "/demo/gate", title: "Follow each payment check.", description: "Trace a request through identity, mandate, and risk checks to see how AgentGate reaches a decision.", preview: "Evaluation stages" },
] as const;

type Destination = (typeof destinations)[number];

function Preview({ destination }: { destination: Destination }) {
  return <>
    <div className="home-preview-heading"><h2>{destination.preview}</h2><span>Example</span></div>
    {destination.route === "/demo" && <>
      <div className="home-amount">₹1,850<span>.00</span></div>
      <p className="home-payee">Electricity Board</p>
      <PaymentPath />
      <div className="home-ledger"><span>Bank ledger entry</span><strong>&#8377;1,850.00</strong></div>
    </>}
    {destination.route === "/demo/bank" && <>
      <div className="home-amount">₹5,000</div><p className="home-payee">Per-payment limit</p>
      <MandateScale />
      <dl className="home-details">
        <div><dt>Agent</dt><dd>BillPal</dd></div>
        <div><dt>Allowed payee</dt><dd>Electricity Board</dd></div>
        <div><dt>Confirmation required</dt><dd>Above ₹4,000</dd></div>
      </dl>
    </>}
    {destination.route === "/demo/agent" && <div className="home-chat">
      <div><strong>You</strong><p>Pay my electricity bill of ₹1,850.</p></div>
      <div><strong>BillPal</strong><p>I’ll send the ₹1,850 request for Electricity Board to AgentGate to check it against your mandate.</p></div>
    </div>}
    {destination.route === "/demo/gate" && <ol className="home-stages">
      {pipeline.map(([key, label], index) => <li key={key} style={{ "--delay": (index * .14) + "s" } as CSSProperties} className={key === "mandate" ? "home-stage-highlight" : undefined}><span>{String(index + 1).padStart(2, "0")}</span>{label}</li>)}
    </ol>}
  </>;
}

export function HomePage() {
  const [selection, setSelected] = useState<number | null>(null);
  const selected = selection ?? 0;
  const preview = useRef<HTMLElement>(null);
  useEffect(() => {
    const panel = preview.current;
    if (!panel || typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver(entries => {
      if (entries.some(entry => entry.isIntersecting)) {
        panel.dataset.visible = "true";
        observer.disconnect();
      }
    }, { threshold: .25 });
    observer.observe(panel);
    return () => observer.disconnect();
  }, []);
  const buttons = useRef<(HTMLButtonElement | null)[]>([]);
  const active = destinations[selected];

  function selectByKey(event: KeyboardEvent<HTMLButtonElement>, index: number) {
    const count = destinations.length;
    const next = { ArrowLeft: (index + count - 1) % count, ArrowRight: (index + 1) % count, Home: 0, End: count - 1 };
    if (!Object.hasOwn(next, event.key)) return;
    event.preventDefault();
    const target = next[event.key as keyof typeof next];
    setSelected(target);
    buttons.current[target]?.focus();
  }

  return <main className="home-page">
    <div className="home-wrap">
      <header className="home-header">
        <span className="home-brand">AgentGate</span>
        <div role="tablist" aria-label="Destinations" className="home-switch"
          style={{ "--index": selected, "--column": selected % 2, "--row": Math.floor(selected / 2) } as CSSProperties}>
          <span className="home-pill" aria-hidden="true" />
          {destinations.map(({ name }, index) => <button key={name} id={`home-tab-${index}`}
            type="button" role="tab" aria-selected={selected === index} aria-controls="home-hero"
            tabIndex={index === selected ? 0 : -1} ref={node => { buttons.current[index] = node; }}
            onKeyDown={event => selectByKey(event, index)} onClick={() => setSelected(index)}>{name}</button>)}
        </div>
        <span className="home-version">Contract v{CONTRACT_VERSION}</span>
      </header>
      <section id="home-hero" role="tabpanel" aria-labelledby={`home-tab-${selected}`} className="home-hero" data-switched={selection !== null}>
        <div className="home-stack">
          {destinations.map((destination, index) =>
            <div key={destination.route} className="home-copy home-layer" aria-hidden={selected !== index} inert={selected !== index}>
              <h1>{destination.title}</h1><p>{destination.description}</p>
              <a className="home-action" href={destination.route}>Open {destination.name} <span aria-hidden="true">&rarr;</span></a>
            </div>
          )}
        </div>
        <aside ref={preview} className="home-preview home-stack" aria-label={active.preview}>
          {destinations.map((destination, index) =>
            <div key={destination.route} className="home-layer" aria-hidden={selected !== index} inert={selected !== index}>
              <Preview destination={destination} />
            </div>
          )}
        </aside>
      </section>
      <Walkthrough />
    </div>
  </main>;
}

export default HomePage;
