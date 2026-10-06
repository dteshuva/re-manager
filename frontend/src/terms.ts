// UK/US vocabulary for the same underlying data.
//
// This app was built against a US multifamily portfolio and its language shows it: "lease",
// "security deposit", "month-to-month", "vacancy". A British landlord lets property under
// different words for the identical concepts — and in a couple of cases the US word names
// something that doesn't exist here, which makes the screen read as though it belongs to
// somebody else's business.
//
// Switched on the account's CURRENCY rather than a new region setting (see `isUK` in ui.ts):
// an account keeping its books in £ is letting property in Britain, and adding a second
// setting that could contradict the first would be worse than deriving it. Nothing here
// touches the API, the database or any computation — these are labels. The wire format stays
// `lease` / `security_deposit` / `is_vacant` in both locales, so one vocabulary can't fork the
// schema, and a US account keeps the language it already had.
//
//   US                      UK                      why
//   ----------------------  ----------------------  ----------------------------------------
//   lease                   tenancy                 an AST is a tenancy; "lease" in Britain
//                                                   implies a long leasehold interest, which
//                                                   is a different thing entirely
//   security deposit        tenancy deposit         the statutory term; it must also sit in
//                                                   an authorised protection scheme
//   month-to-month          periodic                a statutory periodic tenancy — what an
//                                                   AST becomes after its fixed term
//   vacancy / vacant        void                    "void" and "void period" are what a UK
//                                                   landlord and every UK agent call it
//   concession              — (not a UK concept)    a standing monthly rent discount isn't how
//                                                   British lettings work; `hasConcessions`
//                                                   below hides the field entirely rather than
//                                                   offering a translated name for something a
//                                                   UK landlord would never record
//   escalation              rent review             a UK rent rise is a review served by
//                                                   notice, not a contractual escalator
import { isUK } from "./ui";

type Term =
  | "lease"
  | "Lease"
  | "leases"
  | "Leases"
  | "securityDeposit"
  | "periodic"
  | "vacant"
  | "Vacant"
  | "vacancy"
  | "Vacancy"
  | "concession"
  | "Concession"
  | "escalation"
  | "Escalation";

const UK: Record<Term, string> = {
  lease: "tenancy",
  Lease: "Tenancy",
  leases: "tenancies",
  Leases: "Tenancies",
  securityDeposit: "Tenancy deposit",
  periodic: "periodic",
  vacant: "void",
  Vacant: "Void",
  vacancy: "void",
  Vacancy: "Void",
  concession: "rent discount",
  Concession: "Rent discount",
  escalation: "rent review",
  Escalation: "Rent review",
};

const US: Record<Term, string> = {
  lease: "lease",
  Lease: "Lease",
  leases: "leases",
  Leases: "Leases",
  securityDeposit: "Security deposit",
  periodic: "month-to-month",
  vacant: "vacant",
  Vacant: "Vacant",
  vacancy: "vacancy",
  Vacancy: "Vacancy",
  concession: "concession",
  Concession: "Concession",
  escalation: "escalation",
  Escalation: "Escalation",
};

/** The account's word for a concept. `t("Lease")` → "Tenancy" on a £ account, "Lease" on a $ one. */
export const t = (term: Term): string => (isUK() ? UK : US)[term];

/**
 * Whether this account's lettings use standing monthly rent concessions.
 *
 * False in the UK: a British tenancy doesn't carry an ongoing agreed discount off the headline
 * rent — the rent is the rent, and a reduction is a rent reduction. So the field is HIDDEN for a
 * £ account rather than renamed, because a translated label would still be asking for something
 * the operator has no business recording. The column and the whole waterfall split stay intact
 * for US accounts, and the stored value (always null here) still nets correctly out of arrears
 * for anyone who does use it.
 */
export const hasConcessions = () => !isUK();
