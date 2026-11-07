# Export validation

The source export passed all 52 existing automated tests from its own directory using local intercepted browser fixtures and fictitious accounts. The test runner reused installed third-party packages from an existing Python environment. Creating a fresh environment and downloading dependencies was not exercised.

A separate startup check confirmed an empty account catalog, disabled search until account setup, imports resolved inside this export, and distinct runtime/credential namespaces. A temporary mocked credential store was used. No real accounts, appointment queries, submissions or browser sessions were used.

The privacy review covered every exported text file for known personal names/identifiers, user-specific absolute paths, private-key markers and common token patterns. The included ICO and WAV were checked for their expected binary container structure. All original source-file hashes matched the pre-export baseline.

The export physically omits build directories, executable bundles, virtual environments, development work files, databases, logs, screenshots, authenticated sessions, background selections and browser profiles. All remaining account/identity values in tests are fictitious.

Live authentication, current portal behavior, real appointment availability and booking confirmation require separate authorized validation. Automated real reservation confirmation remains unverified.

Windows, a separately installed Brave browser and the dependencies in requirements.txt are required. A source license still needs the owner's selection. Binary distribution needs its own transitive dependency/license review.
