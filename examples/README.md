# Examples

| Example | What it shows | Needs |
| --- | --- | --- |
| [`quickstart/`](quickstart/) | One `@kanode` function node and one `kanode(...)` Gemini agent, each in its own worker | A Kubernetes cluster, Vertex AI |
| [`gke-sandbox/`](gke-sandbox/) | A code interpreter: Gemini writes Python and a single-use GKE Sandbox (gVisor) pod runs it | GKE with node pool auto-creation and network policy, Vertex AI |
| [`swarm/`](swarm/) | A dynamic agent swarm: a planner picks investigator agents with Google Search per request and `ctx.run_node` runs each in its own worker | A Kubernetes cluster, Vertex AI |

Start with the quickstart. All examples can also run locally through their tests,
without a cluster.

Shared files:

| File | Contents |
| --- | --- |
| [`Dockerfile`](Dockerfile) | Image for the workflow host and the workers. Packages the quickstart by default; `AGENT_SRC` and `AGENT_NAME` select another example |
| [`k8s.yaml`](k8s.yaml) | ServiceAccounts, RBAC and the `adk web` Deployment for the workflow host |
