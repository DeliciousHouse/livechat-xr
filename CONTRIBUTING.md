# Contributing to LiveChat XR

Small, source-grounded changes are welcome. Start with the [architecture](docs/architecture.md), [reference](docs/reference.md) and [local development guide](docs/development.md).

## How to propose a change

1. Read the current implementation and tests for the path you want to change. Explain the user-visible problem or task; do not promise unsupported headset behavior.
2. Work on a feature branch from fresh main. Keep changes scoped. Add or extend a unittest that fails before the change when behavior changes.
3. Run the commands in [development](docs/development.md). Build the Windows layer before claiming native tests passed; report skips explicitly. Keep docs, config defaults and UI labels consistent.
4. Open a draft pull request with the purpose, changed surfaces, exact checks/results and any unverified platform or delivery behavior. Include a screenshot for UI changes with no private data. Required CI and the assigned reviewer must approve before merge; do not merge your own draft as proof it works.

Windows app/installer build and tag-release instructions live with the [release/deploy owner](docs/operations.md#release-and-deploy-owner). Do not publish tags or deploy the relay as a contribution test. Run only isolated local services/tests; do not use a production registration or webhook as a fixture.

## Documentation

Use plain Markdown in `docs/`, with README links. Keep reference (what interfaces accept), explanation (why the design exists), how-to (a concrete task) and tutorial (first working result) distinct. Run the docs link test when adding links. User instructions should use visible button names and show how to verify the outcome.

A Discord receipt, source test or simulated screenshot is not proof of a real in-headset notification. State source/version and limitations once at the relevant checkpoint. Do not manufacture output or screenshot UI that is not built.

## Security and support

Never commit `.env`, config with credentials, admin/manage URLs, Discord webhooks, signing keys, OAuth values, Stripe secrets, user emails or raw registrations/log dumps. Treat backups as private. Use placeholders in prose, not working-looking secrets. For a security concern, do not publish exploit details or credentials in a public issue; use GitHub's private vulnerability reporting option if available, or seek a private maintainer contact before disclosing details. Ordinary issues can include version, platform and sanitized reproduction steps.

Only add game compatibility claims backed by a real game/runtime/headset test. Respect game anti-cheat rules; bypassing them is not supported.

## License

Contributions use the repository's [MIT license](LICENSE). Bundled components retain their licenses; see [third-party notices](THIRD-PARTY-NOTICES.md). LiveChat XR is not affiliated with TikTok, Twitch, Meta or Khronos.
