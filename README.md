# pester

A small, generic service for asynchronously asking people things.

Producers submit self-contained interaction jobs: a prompt, evaluation instructions, and delivery constraints. Pester decides when to deliver each one, sends it through a messaging channel, captures the reply, evaluates it with an LLM (or rules), sends feedback in a configurable personality, and exposes everything as a cursor-polled event stream.

```text
Jobs in. Humans bothered. Responses evaluated. Events out.
```

Pester is domain-agnostic. It doesn't know about quizzes, habits, or reminders; that meaning belongs to producers.

- **Spec:** [docs/spec.md](docs/spec.md)
- **Roadmap:** milestone issues M0–M6 on GitHub

Status: pre-alpha, spec accepted, implementation starting.
