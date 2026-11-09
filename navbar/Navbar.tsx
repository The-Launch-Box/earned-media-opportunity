import type { ReactNode } from "react";

import logoUrl from "./tlb-arrow.png";
import "./navbar.css";

/** Home of the Launch Box OS. The brand and the back arrow both go here. */
export const OS_HOME = "https://os.thelaunchbox.com";

/**
 * Portal top bar, extracted from AppShell so another app can drop this folder
 * in. The wordmark and the section back arrow link to os.thelaunchbox.com.
 * Pass buttons or menus as children; they land on the right.
 *
 * Needs the files in this folder kept together: navbar.css, tlb-arrow.png,
 * and fonts/BebasNeuePro-BoldItalic.otf.
 */
export function Navbar({
  section,
  homeHref = OS_HOME,
  logoSrc = logoUrl,
  className,
  children,
}: {
  /** Page name shown after the wordmark, with a back arrow to the OS. */
  section?: string;
  /** Brand and back-arrow destination. */
  homeHref?: string;
  logoSrc?: string;
  className?: string;
  children?: ReactNode;
}) {
  return (
    <header className={className ? `tlb-navbar ${className}` : "tlb-navbar"}>
      <div className="tlb-navbar__row">
        <a className="tlb-navbar__brand" href={homeHref} title="The Launch Box OS">
          <img className="tlb-navbar__mark" src={logoSrc} alt="The Launch Box" />
          <span className="tlb-navbar__word">ACCELERATED BY THE LAUNCH BOX</span>
        </a>

        {section ? (
          <span className="tlb-navbar__section">
            <span className="tlb-navbar__rule" aria-hidden="true" />
            <span className="tlb-navbar__crumb">
              <a
                className="tlb-navbar__back"
                href={homeHref}
                aria-label="Back to The Launch Box OS"
                title="Back to The Launch Box OS"
              >
                <BackArrow />
              </a>
              <span className="tlb-navbar__section-label">{section}</span>
            </span>
          </span>
        ) : null}

        <div className="tlb-navbar__actions">{children}</div>
      </div>
    </header>
  );
}

function BackArrow() {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="m12 19-7-7 7-7" />
      <path d="M19 12H5" />
    </svg>
  );
}
