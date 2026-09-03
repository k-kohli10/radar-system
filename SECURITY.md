# 🔒 Security Policy

Thanks for helping keep RADAR and its users safe. This document explains which
versions receive security fixes and how to report a vulnerability privately.
RADAR's architecture decisions (including how secrets and inter-agent
communication are handled) are recorded as [ADRs](docs/adr/); see
[docs/implementation_plan.md](docs/implementation_plan.md) for the full picture.

---

## Contents

- [Supported Versions](#-supported-versions)
- [Reporting a Vulnerability](#-reporting-a-vulnerability)
- [What to Expect](#-what-to-expect)
- [Coordinated Disclosure](#-coordinated-disclosure)

---

## 📦 Supported Versions

RADAR follows product-level semantic versioning. Security fixes land on the
latest `1.0.x` line and ship in a patch release.

| Version | Supported          |
|---------|--------------------|
| 1.0.x   | ✅ security fixes   |
| < 1.0   | ⬆️ please upgrade to 1.0.x |

## 🐛 Reporting a Vulnerability

Please report security issues **privately** so a fix can ship before details go
public. Use either channel:

- **Preferred: GitHub private advisory.** Open a report at
  <https://github.com/k-kohli10/radar-system/security/advisories/new>. This keeps
  the discussion private to you and the maintainer and produces a coordinated
  advisory when the fix lands.
- **Email:** reach the maintainer at **kohli22k@gmail.com** with the details
  below.

Please include what you can:

- the affected service or package and version (or commit SHA),
- a description of the impact and how it could be exploited,
- steps to reproduce (an alert payload, a `make` command, or logs), and
- any suggested remediation you have in mind.

## ⏱️ What to Expect

- **Acknowledgement** within **3 business days** of your report.
- An initial **assessment and severity** within **7 business days**.
- **Regular updates** as the fix progresses, and credit in the published advisory
  once it lands (opt out any time).

## 🤝 Coordinated Disclosure

Please keep the report private until a fix is released and the advisory is
published. Working together on the disclosure timeline protects everyone
running RADAR while the patch is prepared.
