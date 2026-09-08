# essapps

Framework for ESS data-reduction applications: run records and references, sessions with warm workflows, a small scheduler, and a data store, as sketched in `docs/developer/architecture.md`.

Local mode only: client, backend, launcher, session, and data store in one Python process, with a subprocess launcher as the throwaway execution shape.
