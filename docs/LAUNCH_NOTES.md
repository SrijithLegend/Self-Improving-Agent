# Launch notes: default choices made while working through LAUNCH_SPEC.md

Every place where the spec left room, and what was picked. Change any of these freely.

## Task 1: one product

- **H1** is the spec's example, word for word: "Ship prompt changes only when your own test cases prove they help."
- **How it works** shows the five steps with result fields from the README demo run (20 October 2026). They're
  labelled "real result" and are result fields, not raw terminal output, because the demo's full console log
  wasn't kept.
- **Hero diagram** is now the five-step loop (capture, feedback, dataset, eval, release), with the same single
  animation: an amber square moving along it, off under reduced motion.
- **Results** keeps five rows of the README table and the full caveat paragraph.
- **Pricing** at this point has two cards (self-host, hosted beta); Task 7 replaces it with the four plans.
- **"Works with"** provider marks stay: the SDK traces any model, and `sia improve` drafts candidates with these
  providers.
- **OG image** regenerated with the new headline and the five steps.

## Task 2: request access

- **Email check** is a basic `x@y.z` pattern (no DNS lookup), with surrounding whitespace stripped and the address
  lower-cased before the duplicate check.
- **Duplicates**: only a *pending* request with the same email is de-duplicated. Once a request is approved or
  rejected, the same address can ask again.
- **Rate limit** is 5 requests per IP per hour, counted in memory per process like the other limiters. Requests
  that fail validation (422) don't count, because they never reach the handler.
- **Honeypot**: a field named `website`, positioned off-screen (not `display: none`, which some bots skip) and
  hidden from screen readers. A filled-in honeypot logs a `access_request_honeypot` security event.
- **Approve** names the organization after the company, or the person's name when no company was given, and
  returns the first full-scope key once. An already-decided request returns 404, so a double approval can't
  create two organizations.
- **Admin listing** is also logged as a security event (`access_requests_listed`), since it reveals personal data.
- **Form copy** asks people not to include their users' personal data in the use-case field.
- **Notify me** (the old `mailto:` waitlist) is gone: the Request access form replaces it.
