# Notice

Copyright (C) 2026 Vikas Sahani.

AgentCoSign is free software: you can redistribute it and modify it under the terms of the GNU Affero General Public License, version 3 only (AGPL-3.0-only). See [LICENSE](LICENSE). It comes with no warranty.

**This licence covers only the original work in this repository.** It claims no ownership of, and gives no rights in, anything listed below. Each item stays the property of its owners, under its own terms.

Commits published before October 31, 2026 were released under the MIT License; anyone who took a copy then keeps those rights for that copy.

## Third-party software

Installed as dependencies, not copied into this repository.

| Package | Licence |
|---|---|
| FastAPI | MIT |
| Uvicorn | BSD-3-Clause |
| Pydantic, pydantic-settings | MIT |
| PyYAML | MIT |
| Requests | Apache-2.0 |
| HTTPX | BSD-3-Clause |
| python-dotenv | BSD-3-Clause |
| OpenAI Python client | Apache-2.0 |
| py_webauthn (`webauthn`) | BSD-3-Clause |

Their own dependencies, such as cryptography, certifi and Starlette, keep their own licences, which are stated in each package. Development tools (pytest, Hypothesis, Ruff, mypy, Bandit, pip-audit, gitleaks) are not part of the running product.

## Third-party services and models

Used through their public APIs under their own terms. None of them is included in or redistributed by this repository.

| Service or model | Owner | How it is used |
|---|---|---|
| PayPal REST APIs and sandbox (OAuth, Orders, Payments, Disputes, Webhooks) | PayPal | Orders, refunds, disputes and webhook verification, sandbox only |
| Groq inference | Groq | Runs the models below |
| `openai/gpt-oss-120b`, `openai/gpt-oss-20b`, `openai/gpt-oss-safeguard-20b` | OpenAI | Refund assistant and safety screening |
| `qwen/qwen3.8-27b` | Alibaba Cloud (Qwen) | Reading the customer's words |
| `meta-llama/llama-prompt-guard-2-86m` | Meta | Prompt guard |
| Render | Render | Hosts the public demo |
| cron-job.org | cron-job.org | Keeps the free demo awake |
| GitHub, GitHub Actions | GitHub | Code hosting and CI |
| YouTube | Google | Hosts the demo video |

## Trademarks

PayPal, Groq, OpenAI, Meta, Llama, Qwen, Alibaba Cloud, Render, GitHub, YouTube, Figma, Anthropic, Claude and other names are trademarks of their owners. They appear only to identify the services and tools used. AgentCoSign is an independent project, not affiliated with, sponsored by or endorsed by any of them.

## Use of AI

**To build it.** Vikas Sahani built this project alone, with Claude Code (Anthropic, Claude Pro plan) as the engineering partner. Vikas defined the problem and the product, decided the rules and limits, approved each specification and kept every action with real consequences: accounts and keys, deployment, repository visibility, and each passkey approval. Claude Code researched options, wrote and revised code, tests and documentation drafts, ran test suites and drove the browser and phone during acceptance runs. It held no PayPal or model credentials and could not approve anything. Its work passed the same automated gates as everything else, and safety checks were mutation-tested.

**To design the concept.** The clickable merchant app concept linked from the README was generated with Figma Make from prompts written with Claude Code, and reviewed by Vikas. It is a design concept with sample data, not part of the working product.

**For images.** The README banner and other image assets were generated with ChatGPT (OpenAI) from Vikas's direction.

**Inside the product.** The AI models listed above propose refunds, screen messages and read the customer's words. They never approve or send money: a deterministic policy engine decides and a person co-signs above the merchant's limit.
