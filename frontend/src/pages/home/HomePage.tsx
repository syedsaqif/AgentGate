import { CONTRACT_VERSION } from "../../api/types";
import "./home.css";

export function HomePage() {
  return (
    <div className="home-page">
      <header className="home-header">
        <div className="home-brand">
          <span className="home-logo">AgentGate</span>
          <span className="home-version">Contract v{CONTRACT_VERSION}</span>
        </div>
      </header>

      <section className="home-hero">
        <h1>Autonomous Financial Safety Gateway</h1>
        <p>
          Cryptographic enforcement, risk analysis, and mandate boundary protection
          for AI agent transactions.
        </p>
      </section>

      <div className="home-grid">
        <a href="/demo" className="home-card stage">
          <div className="home-card-top">
            <div className="home-card-icon" aria-hidden="true">🎬</div>
            <span className="home-card-badge">Full Interactive Demo</span>
          </div>
          <h2>Presenter Stage</h2>
          <p>
            Experience the complete 3-pane real-time view: BillPal AI Agent,
            the 10-stage AgentGate policy pipeline, and the live customer bank ledger.
          </p>
          <div className="home-card-action">
            Open Presenter Stage <span>→</span>
          </div>
        </a>

        <a href="/demo/bank" className="home-card bank">
          <div className="home-card-top">
            <div className="home-card-icon" aria-hidden="true">🏦</div>
            <span className="home-card-badge">Customer Portal</span>
          </div>
          <h2>Demo Bank</h2>
          <p>
            Inspect your account balance, create and revoke agent mandates, review
            step-up approvals with PIN, and download offline verifiable QR receipts.
          </p>
          <div className="home-card-action">
            Open Demo Bank <span>→</span>
          </div>
        </a>

        <a href="/demo/agent" className="home-card agent">
          <div className="home-card-top">
            <div className="home-card-icon" aria-hidden="true">🤖</div>
            <span className="home-card-badge">AI Assistant & Lab</span>
          </div>
          <h2>Payment Assistant</h2>
          <p>
            Interact directly with BillPal through conversational payment prompts,
            or test prompt injection, category spoofing, and attack vectors in the Test Lab.
          </p>
          <div className="home-card-action">
            Open Payment Assistant <span>→</span>
          </div>
        </a>

        <a href="/demo/gate" className="home-card gate">
          <div className="home-card-top">
            <div className="home-card-icon" aria-hidden="true">🛡️</div>
            <span className="home-card-badge">Pipeline Visualizer</span>
          </div>
          <h2>Payment Gate</h2>
          <p>
            Observe the 10-stage evaluation and safety pipeline: Intake, Identity,
            Agent DPoP, Request, Mandate, Policy, Risk signals, Breaker, and Bank execution.
          </p>
          <div className="home-card-action">
            Open Payment Gate <span>→</span>
          </div>
        </a>
      </div>

      <section className="home-guide">
        <h3>How to Explore</h3>
        <div className="home-guide-steps">
          <div className="home-guide-step">
            <span className="home-guide-num">1</span>
            <div className="home-guide-text">
              <h4>Setup Mandate</h4>
              <p>In <strong>Demo Bank</strong>, log in with PIN <code>4821</code> to view or adjust your agent spending limits.</p>
            </div>
          </div>
          <div className="home-guide-step">
            <span className="home-guide-num">2</span>
            <div className="home-guide-text">
              <h4>Run Scenarios</h4>
              <p>In <strong>Presenter Stage</strong>, select normal payments or security attacks to watch each gate evaluate in real time.</p>
            </div>
          </div>
          <div className="home-guide-step">
            <span className="home-guide-num">3</span>
            <div className="home-guide-text">
              <h4>Verify Receipts</h4>
              <p>After an executed payment, download the cryptographic QR code for independent offline verification.</p>
            </div>
          </div>
        </div>
      </section>
    </div>
  );
}

export default HomePage;
