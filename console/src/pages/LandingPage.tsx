import { useEffect, useRef } from "react";

import "./LandingPage.css";

const pilotHref =
  "mailto:hello@pacha.co.ke?subject=Pacha%20pilot%20request&body=I%27d%20like%20to%20run%20a%20claim%20through%20Pacha.";

type FieldProps = {
  label: string;
  value: string;
  wide?: boolean;
};

function DocumentField({ label, value, wide = false }: FieldProps) {
  return (
    <div className={wide ? "landing-doc-field landing-doc-field-wide" : "landing-doc-field"}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function Signature({ label, path }: { label: string; path: string }) {
  return (
    <div className="landing-signature">
      <svg viewBox="0 0 72 18" aria-hidden="true">
        <path d={path} />
      </svg>
      <span>{label}</span>
    </div>
  );
}

function ClaimForm() {
  return (
    <>
      <div className="landing-doc-letterhead">
        <div>
          <b>Umoja General Insurance Co. Ltd</b>
          <small>P.O. Box 30712-00100, Nairobi · Tel 020 271 4400 · claims@umojagen.co.ke</small>
        </div>
        <code>FORM CF-M01</code>
      </div>
      <div className="landing-doc-title">Motor vehicle accident claim form</div>
      <div className="landing-doc-grid landing-doc-grid-form">
        <DocumentField label="Name of insured" value="WANJIRU NJERI KAMAU" />
        <DocumentField label="ID No." value="24581903" />
        <DocumentField label="KRA PIN" value="A004521873B" />
        <DocumentField label="Telephone" value="0722 481 907" />
        <DocumentField label="Policy No." value="MTR/PRV/0044821" />
        <DocumentField label="Cover" value="COMPREHENSIVE" />
        <DocumentField label="Reg No. / Make" value="KDA 412X — TOYOTA AXIO 2017" />
        <DocumentField label="Date / time of accident" value="14/03/2026 · 17:40" />
        <DocumentField label="Reported at" value="KILIMANI POLICE STATION" />
        <DocumentField label="OB No." value="47/14/03/2026" />
        <DocumentField
          label="Brief description of accident"
          value="REAR-ENDED WHILE STATIONARY AT TRAFFIC SIGNAL, NGONG RD / RING RD JUNCTION. THIRD PARTY ADMITS FAULT."
          wide
        />
      </div>
      <div className="landing-doc-signoff">
        <small>I declare that the above particulars are true and correct to the best of my knowledge.</small>
        <Signature
          label="Signature of insured · 16/03/2026"
          path="M2 13 C 10 2, 16 16, 24 8 S 40 2, 46 10 S 60 16, 70 6"
        />
      </div>
    </>
  );
}

function HandlerEmail() {
  return (
    <div className="landing-email">
      <div className="landing-email-actions">
        <span>↩ Reply</span><span>↩↩ Reply All</span><span>↪ Forward</span><span>⋯</span>
      </div>
      <div className="landing-email-body">
        <strong>RE: RE: FW: Claim MTR/PRV/0044821 — documents still outstanding</strong>
        <div className="landing-email-sender">
          <span className="landing-email-avatar">CD</span>
          <span><b>Claims Desk &lt;claims.desk@umojagen.co.ke&gt;</b><small>To: Wanjiru Njeri Kamau; Cc: Sawa Insurance Brokers Ltd</small></span>
          <time>Tue 24/03/2026 09:12</time>
        </div>
        <div className="landing-email-copy">
          <p>Dear Ms Kamau,</p>
          <p>Further to our letter of 18th March, we are still awaiting the following before your claim can proceed:</p>
          <ol>
            <li>Original police abstract (certified copy accepted)</li>
            <li>Copy of vehicle logbook</li>
            <li>Copy of driving licence and KRA PIN</li>
            <li>Claim form — page 2 was not legible, kindly resubmit</li>
          </ol>
          <p>Please note your claim remains <b>PENDING</b> until all documents are received in full.</p>
          <p className="landing-email-closing">Kind regards,<br />Grace Odhiambo · Claims Department<br />Umoja General Insurance Co. Ltd</p>
        </div>
      </div>
    </div>
  );
}

function PoliceAbstract() {
  return (
    <>
      <div className="landing-police-heading">
        <strong>Republic of Kenya</strong>
        <b>National Police Service</b>
        <small>“Utumishi kwa Wote”</small>
      </div>
      <div className="landing-doc-title">B — Abstract from police records</div>
      <div className="landing-doc-grid">
        <DocumentField label="Abstract register No." value="118/2026" />
        <DocumentField label="Misc. receipt No." value="88213 · KES 100" />
        <DocumentField label="Police reference" value="KIL/TRF/47/2026" />
        <DocumentField label="OB No. / date" value="47/14/03/2026 · 14/03/2026" />
        <DocumentField
          label="We have confirmed the report of"
          value="TRAFFIC ACCIDENT INVOLVING MV KDA 412X AND MV KCF 903T ALONG NGONG ROAD, WHICH WAS REPORTED AT KILIMANI POLICE STATION."
          wide
        />
        <DocumentField label="Name of complainant" value="WANJIRU NJERI KAMAU" />
        <DocumentField label="Remarks" value="BLAME ON DRIVER OF MV KCF 903T. NO INJURIES." />
      </div>
      <div className="landing-police-signoff">
        <div className="landing-police-stamp">Kilimani<br />Police Station<br />• 26 Mar 2026 •</div>
        <Signature
          label="Officer i/c · Sgt. P. Mwangi · No. 102447"
          path="M2 12 C 12 16, 14 3, 26 9 S 44 15, 50 7 S 62 3, 70 11"
        />
      </div>
      <small className="landing-police-note">This Abstract Form is free of charge.</small>
    </>
  );
}

const assessmentLines = [
  ["REAR BUMPER ASSEMBLY", "41,200"],
  ["BOOT LID — REPLACE & RESPRAY", "63,800"],
  ["REAR BODY PANEL — BEATING", "18,400"],
  ["LABOUR (18 HRS)", "27,000"],
  ["SUNDRIES & CONSUMABLES", "3,600"],
];

function AssessorReport() {
  return (
    <>
      <div className="landing-doc-letterhead">
        <div>
          <b>Mashariki Motor Assessors Ltd</b>
          <small>Licensed Motor Assessors & Valuers · P.O. Box 4410-00506, Nairobi</small>
        </div>
        <code>REF MMA/2026/0761<br />26/03/2026</code>
      </div>
      <div className="landing-doc-grid">
        <DocumentField label="Vehicle" value="KDA 412X — TOYOTA AXIO 2017" />
        <DocumentField label="Odometer" value="84,112 KM" />
        <DocumentField label="Insurer / policy" value="UMOJA GENERAL · MTR/PRV/0044821" />
        <DocumentField label="Pre-accident value" value="KES 1,150,000" />
      </div>
      <div className="landing-assessment-table">
        <div className="landing-assessment-heading"><b>Damage assessment</b><b>KES</b></div>
        {assessmentLines.map(([description, value]) => (
          <div key={description}><span>{description}</span><span>{value}</span></div>
        ))}
        <div className="landing-assessment-total"><b>Total repair estimate</b><b>154,000</b></div>
      </div>
      <div className="landing-doc-signoff">
        <p><span>Recommendation:</span><br />REPAIR AUTHORISED. NOT AN ECONOMIC TOTAL LOSS. SALVAGE N/A.</p>
        <Signature
          label="J. Otieno · Principal Assessor"
          path="M3 11 C 9 3, 20 15, 30 7 S 48 4, 54 12 S 64 5, 70 9"
        />
      </div>
    </>
  );
}

const documents = [
  { label: "(01 — MOTOR CLAIM FORM)", className: "landing-paper-claim", content: <ClaimForm /> },
  { label: "(02 — HANDLER INBOX)", className: "landing-paper-email", content: <HandlerEmail /> },
  { label: "(03 — POLICE ABSTRACT)", className: "landing-paper-police", content: <PoliceAbstract /> },
  { label: "(04 — ASSESSOR'S REPORT)", className: "landing-paper-assessor", content: <AssessorReport /> },
];

function HeroFilters({ displacementRefs, turbulenceRefs }: {
  displacementRefs: React.MutableRefObject<(SVGFEDisplacementMapElement | null)[]>;
  turbulenceRefs: React.MutableRefObject<(SVGFETurbulenceElement | null)[]>;
}) {
  const frequencies = ["0.010 0.038", "0.012 0.032", "0.009 0.042", "0.011 0.036"];
  const seeds = [3, 11, 27, 42];
  return (
    <svg className="landing-filter-defs" aria-hidden="true">
      <defs>
        {frequencies.map((frequency, index) => (
          <filter key={frequency} id={`landing-warp-${index}`} x="-30%" y="-30%" width="160%" height="160%">
            <feTurbulence
              ref={(node) => { turbulenceRefs.current[index] = node; }}
              type="turbulence"
              baseFrequency={frequency}
              numOctaves="2"
              seed={seeds[index]}
              result={`landing-noise-${index}`}
            />
            <feDisplacementMap
              ref={(node) => { displacementRefs.current[index] = node; }}
              in="SourceGraphic"
              in2={`landing-noise-${index}`}
              scale="0"
              xChannelSelector="R"
              yChannelSelector="G"
            />
          </filter>
        ))}
      </defs>
    </svg>
  );
}

const facts = [
  ["(90%)", "of claims-handling work is document transport and rules-checking — moving paper, chasing signatures, re-keying fields."],
  ["(10%)", "is genuine judgement. That is where human claims professionals belong — and where they rarely get to spend their time."],
  ["KES 67.6bn", "paid out annually across Kenyan motor and medical claims — every shilling of it routed through that paperwork."],
];

const steps = [
  ["(01) Execute", "Agents collect the abstract, chase the email thread, request the assessor's report, and assemble the file — from the moment the FNOL lands."],
  ["(02) Validate", "Software cross-checks every field against the policy, the tariff, and the rules — flagging gaps before a human ever opens the file."],
  ["(03) Decide", "Your claims professionals receive one complete, approval-ready pack. They spend their time on judgement — nothing else."],
];

export function LandingPage() {
  const runwayRef = useRef<HTMLDivElement>(null);
  const cardRefs = useRef<(HTMLDivElement | null)[]>([]);
  const documentRefs = useRef<(HTMLDivElement | null)[]>([]);
  const displacementRefs = useRef<(SVGFEDisplacementMapElement | null)[]>([]);
  const turbulenceRefs = useRef<(SVGFETurbulenceElement | null)[]>([]);
  const wordmarkRef = useRef<HTMLHeadingElement>(null);
  const taglineRef = useRef<HTMLDivElement>(null);
  const cueRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const oldTitle = document.title;
    document.title = "Pacha — Claims work, completed";
    const runway = runwayRef.current;
    const reducedMotion = typeof window.matchMedia === "function" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    let animationFrame = 0;
    const starts = [0.1, 0.22, 0.34, 0.46];
    const rotations = [-2, 1.5, 1, -1.5];
    const drift = [[-20, -12, -1.2], [18, -14, 1.1], [-16, 14, 0.9], [20, 12, -1.1]];
    const clamp = (value: number, min: number, max: number) => Math.min(max, Math.max(min, value));
    const ease = (value: number) => 1 - Math.pow(1 - value, 3);

    const render = (now: number, forcedProgress?: number) => {
      if (!runway) return;
      const bounds = runway.getBoundingClientRect();
      const scrollProgress = clamp(-bounds.top / Math.max(1, bounds.height - window.innerHeight), 0, 1);
      const progress = forcedProgress ?? scrollProgress;
      const scale = window.innerWidth < 720
        ? Math.min(0.66, Math.max(0.48, window.innerWidth / 620))
        : Math.min(1, window.innerHeight / 900, window.innerWidth / 1300);

      documents.forEach((_, index) => {
        const localProgress = ease(clamp((progress - starts[index]) / 0.32, 0, 1));
        const wobble = localProgress > 0.01 ? Math.sin(now / 1100 + index * 1.7) * 3 * localProgress : 0;
        const [x, y, rotation] = drift[index];
        const card = cardRefs.current[index];
        if (card) {
          card.style.transform = `translate3d(${(x * localProgress).toFixed(1)}px, ${(y * localProgress + wobble).toFixed(1)}px, 0) rotate(${(rotations[index] + rotation * localProgress).toFixed(2)}deg) scale(${scale.toFixed(3)})`;
        }
        const noise = 70 * localProgress;
        displacementRefs.current[index]?.setAttribute("scale", noise.toFixed(1));
        if (noise > 0.5) {
          const xFrequency = (0.01 + 0.004 * Math.sin(now / 900 + index * 2)).toFixed(4);
          const yFrequency = (0.036 + 0.01 * Math.cos(now / 700 + index)).toFixed(4);
          turbulenceRefs.current[index]?.setAttribute("baseFrequency", `${xFrequency} ${yFrequency}`);
        }
        const documentNode = documentRefs.current[index];
        if (documentNode) documentNode.style.opacity = (1 - 0.55 * localProgress).toFixed(2);
      });

      const wordmarkProgress = ease(clamp((progress - 0.05) / 0.6, 0, 1));
      if (wordmarkRef.current) {
        wordmarkRef.current.style.opacity = (0.35 + 0.65 * wordmarkProgress).toFixed(2);
        wordmarkRef.current.style.transform = `scale(${(0.94 + 0.06 * wordmarkProgress).toFixed(3)})`;
        wordmarkRef.current.style.filter = `blur(${(7 * (1 - wordmarkProgress)).toFixed(1)}px)`;
      }
      const taglineProgress = ease(clamp((progress - 0.72) / 0.22, 0, 1));
      if (taglineRef.current) {
        taglineRef.current.style.opacity = taglineProgress.toFixed(2);
        taglineRef.current.style.transform = `translate3d(0, ${(16 * (1 - taglineProgress)).toFixed(1)}px, 0)`;
        taglineRef.current.style.pointerEvents = taglineProgress > 0.5 ? "auto" : "none";
      }
      if (cueRef.current) cueRef.current.style.opacity = (1 - clamp(progress / 0.12, 0, 1)).toFixed(2);
    };

    if (reducedMotion) {
      runway?.classList.add("landing-hero-runway-reduced");
      render(0, 1);
    } else {
      const tick = (now: number) => {
        render(now);
        animationFrame = window.requestAnimationFrame(tick);
      };
      animationFrame = window.requestAnimationFrame(tick);
    }

    return () => {
      window.cancelAnimationFrame(animationFrame);
      document.title = oldTitle;
    };
  }, []);

  return (
    <main className="landing-page" id="main-content">
      <a className="landing-skip-link" href="#claims-floor">Skip to content</a>
      <HeroFilters displacementRefs={displacementRefs} turbulenceRefs={turbulenceRefs} />
      <div ref={runwayRef} className="landing-hero-runway">
        <section className="landing-hero" aria-labelledby="landing-title">
          <div className="landing-hero-orbs" aria-hidden="true">
            <img src="/landing/orb-blue-teal.png" alt="" />
            <img src="/landing/blob-periwinkle-peach.png" alt="" />
          </div>
          <header className="landing-header">
            <span>2026 Pacha</span>
            <a href="#pilot">(Request a pilot)</a>
          </header>
          <div className="landing-document-stage" aria-hidden="true">
            {documents.map((document, index) => (
              <div
                key={document.label}
                ref={(node) => { cardRefs.current[index] = node; }}
                className={`landing-paper ${document.className}`}
              >
                <div className="landing-paper-label">{document.label}</div>
                <div
                  ref={(node) => { documentRefs.current[index] = node; }}
                  className="landing-paper-content"
                  style={{ filter: `url(#landing-warp-${index})` }}
                >
                  {document.content}
                </div>
              </div>
            ))}
          </div>
          <div className="landing-hero-message">
            <h1 ref={wordmarkRef} id="landing-title">Pacha</h1>
            <div ref={taglineRef} className="landing-tagline">
              <p>Every claim arrives as scattered paperwork. Pacha turns an FNOL into a complete, approval-ready claims pack.</p>
              <div className="landing-hero-actions">
                <a className="landing-hero-link" href={pilotHref} aria-label="Request a pilot">(Request a pilot)</a>
              </div>
            </div>
          </div>
          <div ref={cueRef} className="landing-scroll-cue" aria-hidden="true">(Scroll)</div>
        </section>
      </div>

      <section className="landing-section landing-claims-floor" id="claims-floor" aria-labelledby="claims-floor-title">
        <div className="landing-section-kicker">(01) The claims floor</div>
        <h2 id="claims-floor-title">I spent weeks on the claims floor of a Kenyan insurer. This is what I measured.</h2>
        <div className="landing-facts">
          {facts.map(([value, description]) => (
            <article key={value}>
              <strong>{value}</strong>
              <p>{description}</p>
            </article>
          ))}
        </div>
      </section>

      <section className="landing-section-shell" aria-labelledby="how-it-works-title">
        <div className="landing-section">
          <div className="landing-section-kicker">(02) How it works</div>
          <h2 id="how-it-works-title">Agents execute. Software validates. Claims professionals decide.</h2>
          <div className="landing-steps">
            {steps.map(([title, description]) => (
              <article key={title}>
                <h3>{title}</h3>
                <p>{description}</p>
              </article>
            ))}
          </div>
        </div>
      </section>

      <section className="landing-section-shell landing-business" aria-labelledby="business-model-title">
        <img src="/landing/grain-wave-mono.png" alt="" aria-hidden="true" />
        <div className="landing-section">
          <div className="landing-section-kicker">(03) Business model</div>
          <h2 id="business-model-title">We don’t sell a tool. We do the work.</h2>
          <p>Paid per claim processed.<br />Not per seat. Not per hour.</p>
        </div>
      </section>

      <section className="landing-section-shell landing-pilot" id="pilot" aria-labelledby="pilot-title">
        <img src="/landing/spark-coral-pink.png" alt="" aria-hidden="true" />
        <div className="landing-section">
          <h2 id="pilot-title">Run a claim through Pacha.</h2>
          <p>We are piloting with Kenyan motor and medical insurers now. Send one live claim; we return the pack.</p>
          <div className="landing-pilot-actions">
            <a className="landing-button landing-button-primary" href={pilotHref}>Request a pilot</a>
            <a className="landing-button landing-button-ghost" href="mailto:hello@pacha.co.ke">hello@pacha.co.ke</a>
          </div>
          <footer className="landing-footer">
            <span>Pacha</span>
            <span>2026 Pacha — Nairobi</span>
          </footer>
        </div>
      </section>
    </main>
  );
}
