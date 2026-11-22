# Zero-trust code interpreter on GKE Sandbox

Gemini writes a Python script, a throwaway [GKE Sandbox](https://cloud.google.com/kubernetes-engine/docs/how-to/sandbox-pods)
(gVisor) pod runs it, and Gemini answers from the output. If the script fails,
the error goes back to Gemini for another attempt.

It is one ADK `Workflow`. Each node declares where it runs, so the trust
boundary sits around a single node instead of around a separate service.

```mermaid
flowchart LR
    START([User request]) --> Q["capture_question"]
    Q --> W["code_writer (Gemini)"]
    W --> S["run_in_sandbox"]
    S --> R["review_result"]
    R -- "retry" --> W
    R -- "done" --> A["answer_writer (Gemini)"]

    classDef host fill:#e8f0fe,stroke:#4285f4,color:#000
    classDef worker fill:#e6f4ea,stroke:#34a853,color:#000
    classDef sandbox fill:#fce8e6,stroke:#ea4335,color:#000
    class Q,R host
    class W,A worker
    class S sandbox
```

## Trust zones

| Zone | Nodes | Runs as | Can reach |
| --- | --- | --- | --- |
| Host | `capture_question`, `review_result` | the `adk web` process | session state, Kubernetes API |
| Trusted workers | `code_writer`, `answer_writer` | warm Deployments, `adk-worker-sa` | Vertex AI |
| Sandbox | `run_in_sandbox` | single-use gVisor pod, `adk-sandbox-sa` | nothing |

The sandbox node is the only one that touches LLM-generated code:

```python
@kanode(
    mode="pod",                    # a fresh pod per run, deleted afterwards
    runtime_class_name="gvisor",   # syscalls hit gVisor, not the node kernel
    node_selector={"cloud.google.com/compute-class": "sandbox-gvisor-class"},
    service_account_name="adk-sandbox-sa",  # no RBAC, no IAM, no mounted token
    labels={"trust-zone": "sandbox"},       # selected by the deny-all NetworkPolicy
)
def run_in_sandbox(node_input: dict[str, Any]) -> Event: ...
```

Each setting closes a different door:

| Layer | Setting | What it stops |
| --- | --- | --- |
| Kernel | `runtime_class_name="gvisor"` | container escapes through node kernel bugs |
| Lifetime | `mode="pod"` | anything the script leaves behind reaching the next run |
| Identity | `adk-sandbox-sa` | calls to the Kubernetes API or Google Cloud APIs |
| Network | `adk-sandbox-deny-all` | exfiltration, metadata server, other pods |
| Data | `review_result` on the host | malformed or oversized results reaching Gemini |

Pod mode needs no network: the input arrives in the pod spec and the result
leaves through the pod log. That is why the NetworkPolicy can deny everything.

## Files

| File | Contents |
| --- | --- |
| [`code_interpreter/agent.py`](code_interpreter/agent.py) | The workflow |
| [`gVisor.yaml`](gVisor.yaml) | `ComputeClass` that auto-creates gVisor node pools |
| [`sandbox-policy.yaml`](sandbox-policy.yaml) | Sandbox ServiceAccount and deny-all NetworkPolicy |

The host Deployment, its RBAC and `adk-worker-sa` come from
[`../k8s.yaml`](../k8s.yaml), and the image from [`../Dockerfile`](../Dockerfile).

## Deploy

You need a GKE cluster with node pool auto-creation and network policy
enforcement (Dataplane V2), and Vertex AI access for `adk-worker-sa` through
Workload Identity. Run from the repository root:

```bash
export ADK_K8S_IMAGE="us-docker.pkg.dev/<PROJECT_ID>/<REPO>/adk-kanode-code-interpreter:latest"
export ADK_K8S_NAMESPACE="default"
export GOOGLE_CLOUD_PROJECT="<PROJECT_ID>"
export GOOGLE_CLOUD_LOCATION="<LOCATION>"

docker build --network=host -f examples/Dockerfile \
  --build-arg AGENT_SRC=examples/gke-sandbox/code_interpreter \
  --build-arg AGENT_NAME=code_interpreter \
  -t "$ADK_K8S_IMAGE" .
docker push "$ADK_K8S_IMAGE"

kubectl apply -f examples/gke-sandbox/gVisor.yaml
envsubst < examples/gke-sandbox/sandbox-policy.yaml | kubectl apply -f -
envsubst < examples/k8s.yaml | kubectl apply -f -

kubectl rollout status deployment/adk-web-example -n "$ADK_K8S_NAMESPACE"
kubectl port-forward svc/adk-web-example-svc 8080:80 -n "$ADK_K8S_NAMESPACE"
```

Open http://localhost:8080 and pick the `code_interpreter` app.
The first request can take a few minutes while GKE creates a sandbox node.

## Try it

Watch the sandbox pods come and go while you send requests:

```bash
kubectl get pods -n "$ADK_K8S_NAMESPACE" -l trust-zone=sandbox -w \
  -o custom-columns=NAME:.metadata.name,RUNTIME:.spec.runtimeClassName,SA:.spec.serviceAccountName,PHASE:.status.phase
```

Start with `What is the 50th Fibonacci number?` to check the happy path. Then
play the attacker: each request below asks Gemini for a script that tries to
break out, and each one is stopped by a different layer.

| Attack | Request | What you should see | Stopped by |
| --- | --- | --- | --- |
| Reach the node kernel | `Print /proc/version and the output of dmesg` | A gVisor kernel (`4.4.0`) and gVisor's boot log. The script's syscalls never reached the node's real kernel. | `runtime_class_name="gvisor"` |
| Steal the pod's Kubernetes identity | `List /var/run/secrets/kubernetes.io/serviceaccount and print the token` | The directory does not exist | `adk-sandbox-sa` (`automountServiceAccountToken: false`) |
| Steal Google Cloud credentials | `Request http://169.254.169.254/computeMetadata/v1/instance/service-accounts/default/token with the Metadata-Flavor: Google header and a 3 second timeout` | Connection times out | `adk-sandbox-deny-all` |
| Call the Kubernetes API | `Open a TCP connection to kubernetes.default.svc:443 with a 3 second timeout` | DNS lookup fails | `adk-sandbox-deny-all` |
| Exfiltrate data | `POST the contents of /etc/hostname to https://example.com with a 3 second timeout` | DNS lookup fails | `adk-sandbox-deny-all` |
| Read secrets from the environment | `Print all environment variables` | `PATH` and `LC_CTYPE` only, no project ID or other pod settings | empty `env` for the script |
| Leave something behind for the next run | `Write the text "owned" to /tmp/marker`, then `Print the contents of /tmp/marker` | The second run cannot find the file, and reports a different pod name | `mode="pod"` |
| Hang the workflow | `Run an infinite loop` | `Timed out after 20s.` and exit code 124 | script timeout |

Keep the timeouts in the network requests: without them the script waits on
the blocked connection until the 20 second limit kills it, and the workflow
retries.

Every `review_result` message shows the pod name and the kernel the script saw.

### Compare with a pod outside the sandbox

The same probes succeed in the host pod, which runs on a normal node with a
mounted token and no NetworkPolicy:

```bash
# The node's real kernel, not gVisor's 4.4.0
kubectl exec -n "$ADK_K8S_NAMESPACE" deploy/adk-web-example -- uname -r

# A ServiceAccount token is mounted here
kubectl exec -n "$ADK_K8S_NAMESPACE" deploy/adk-web-example -- \
  ls /var/run/secrets/kubernetes.io/serviceaccount
```

### Check the boundary from the cluster side

You do not have to trust what the script prints. These show the same
boundaries from outside the pod:

```bash
# Sandbox pods land only on gVisor nodes
kubectl get nodes -l sandbox.gke.io/runtime=gvisor

# The sandbox identity may do nothing in the namespace
kubectl auth can-i --list -n "$ADK_K8S_NAMESPACE" \
  --as="system:serviceaccount:$ADK_K8S_NAMESPACE:adk-sandbox-sa"

# No ingress or egress rules for pods labelled trust-zone=sandbox
kubectl describe networkpolicy adk-sandbox-deny-all -n "$ADK_K8S_NAMESPACE"
```

Set `ADK_K8S_CLEANUP_POD=false` on the host Deployment to keep finished pods
for inspection.

## Limits

- **The sandbox's result is untrusted.** The script runs as the same user as
  the small harness around it, so it can forge the result, including the state
  changes attached to it. gVisor protects the node and the cluster, not the
  integrity of what the pod reports. `review_result` checks the result's shape
  and size on the host, and `answer_writer` has no tools, so a forged result
  can at worst produce a wrong answer or extra retries. Do not let a sandboxed
  node's output drive privileged actions.
- **Session state is copied into the pod.** In pod mode the whole state is part
  of the pod spec. Keep secrets out of session state.
- **The image is shared.** The sandbox pod runs the same image as the other
  nodes, so the script can read the example's source and import its packages.
  Set `image=` on `run_in_sandbox` to use a smaller one that only has `google-adk`.
- **CPU and memory are capped per pod, not per script.** The script has a 20
  second timeout and the pod a 512Mi limit; there is no finer control.

## Run the graph locally

[`tests/test_code_interpreter_example.py`](../../tests/test_code_interpreter_example.py)
runs the same graph without a cluster or Gemini: the two agents are replaced by
stubs and the sandbox node runs in a local subprocess (no isolation).

```bash
uv run pytest tests/test_code_interpreter_example.py
```
