FROM python:3.13-alpine@sha256:399babc8b49529dabfd9c922f2b5eea81d611e4512e3ed250d75bd2e7683f4b0 AS proof

ARG CONTROL_PLANE_IP
ARG HTTP_PROXY

RUN test -n "$HTTP_PROXY" && python -c \
    "import urllib.request; urllib.request.urlopen('https://example.com', timeout=10).read(1)"
RUN if HTTP_PROXY= HTTPS_PROXY= http_proxy= https_proxy= \
  python -c \
  "import urllib.request; urllib.request.urlopen('https://example.com', timeout=2)"; then \
  echo "public dependency traffic bypassed the egress proxy" >&2; \
  exit 1; \
    fi
RUN if python -c \
  "import urllib.request; urllib.request.urlopen('http://${CONTROL_PLANE_IP}:8000/health', timeout=2)"; then \
  echo "control plane was reachable through egress proxy" >&2; \
      exit 1; \
    fi
RUN if HTTP_PROXY= HTTPS_PROXY= http_proxy= https_proxy= \
  python -c \
  "import urllib.request; urllib.request.urlopen('http://${CONTROL_PLANE_IP}:8000/health', timeout=2)"; then \
  echo "control plane was reachable without egress proxy" >&2; \
  exit 1; \
    fi
RUN if python -c \
  "import urllib.request; urllib.request.urlopen('http://169.254.169.254/latest/meta-data/', timeout=2)"; then \
  echo "cloud metadata was reachable through egress proxy" >&2; \
  exit 1; \
    fi
RUN test ! -S /var/run/docker.sock
RUN printf 'buildkit-isolation-passed\n' > /proof.txt

FROM scratch
COPY --from=proof /proof.txt /proof.txt
