# Contributing to wainlux-pi

Thank you for your interest in contributing! This project maintains strict **contamination control** to ensure clean-room reverse-engineering integrity and legal distribution.

## ⚠️ Critical: Contributor Declaration

By submitting a pull request, you **implicitly declare** that:

1. **You did not copy vendor source code** - Your contribution contains no code copied, derived from, or based on proprietary Wainlux source code
2. **You did not include decompiled fragments** - No code snippets, algorithms, or structures were extracted from decompiled binaries
3. **Your contribution is original** - Code and documentation are your own independent work
4. **You used only clean sources** - Your work is based solely on:
   - Observed device behavior (USB captures, serial monitoring)
   - Published protocol documentation (K3/6 references, community findings)
   - Black-box testing and experimentation
   - Public datasheets (USB-serial chips, ARM MCUs, etc.)

## Acceptable Contribution Sources

✅ **ALLOWED:**
- Independent protocol implementations based on observed behavior
- USB packet captures you created yourself
- Serial port monitoring logs
- Test results from your own K6/K3 hardware
- Corrections to existing protocol documentation based on empirical testing
- Code refactoring, bug fixes, performance improvements
- Documentation improvements, typo fixes
- PlantUML diagrams, architecture improvements

## Prohibited Contribution Sources

❌ **FORBIDDEN:**
- Code copied from decompiled vendor software
- Algorithms or logic structures reverse-engineered from binaries
- Proprietary firmware analysis (unless you own the device and performed independent analysis)
- Leaked vendor documentation or SDKs
- Any material marked "confidential" or "proprietary"
- Third-party code without compatible licensing

## How to Contribute Safely

### 1. Protocol Contributions
If you're contributing protocol findings:

```markdown
**Source:** USB capture from vendor software v1.2.3
**Method:** Wireshark monitoring on Windows 10
**Test Device:** K6 serial #12345
**Finding:** Opcode 0x28 sets power level (range 0-1000)
**Validation:** Tested with powers 100, 500, 1000 - device responds with ACK 0x09
```

### 2. Code Contributions
If you're contributing code:

- Write from scratch based on protocol documentation
- Do NOT reference decompiled code while writing
- Test on actual hardware when possible
- Include comments explaining protocol behavior (not vendor implementation details)

### 3. Documentation Contributions
- CC-BY-4.0 or CC0 dual-license applies (see [LICENSE-DOCS](LICENSE-DOCS))
- Must be your original writing or properly attributed
- Protocol documentation must cite observation methods

## Pull Request Checklist

Before submitting a PR, ensure:

- [ ] Code follows existing style (see scripts/ for examples)
- [ ] No vendor binaries, decompiled code, or proprietary materials included
- [ ] Changes tested on actual hardware (if applicable)
- [ ] Documentation updated (if protocol changes)
- [ ] Commit message explains **what** changed and **why**
- [ ] You can honestly declare your contribution is clean-room

## Contamination Control Process

### If You've Seen Vendor Source Code

**STOP** - Do not contribute protocol implementations if you have viewed:
- Decompiled vendor apps (Java, .NET, native binaries)
- Leaked source code
- Confidential vendor documentation

You may still contribute:
- Hardware setup guides
- Documentation improvements (non-protocol)
- Bug reports (without code fixes)
- Test results and observations

### If You're Unsure

Open an **issue first** describing:
1. What you want to contribute
2. How you derived the information
3. Any vendor materials you consulted

Maintainers will review before you invest time in a PR.

## Testing Requirements

### Protocol Changes
- Must test on actual K6 hardware
- Include before/after behavior description
- Provide sample output or USB captures

### Code Changes
- Python 3.13+ compatible
- Runs in Docker on ARMv6 (Pi Zero W)
- No new dependencies without justification

### Documentation Changes
- AsciiDoc format for technical docs
- Markdown for meta files (like this one)
- PlantUML for diagrams

## License Agreement

- **Code contributions:** MIT License (see [LICENSE](LICENSE))
- **Documentation contributions:** CC-BY-4.0 or CC0 dual-license (see [LICENSE-DOCS](LICENSE-DOCS))

By submitting a PR, you agree to license your contribution under these terms.

## Communication

- **Issues:** Bug reports, feature requests, protocol questions
- **Discussions:** General questions, hardware setup help
- **PRs:** Code and documentation contributions (with declaration above)

## Code of Conduct

- Be respectful and constructive
- Focus on technical merit
- Assume good faith
- Help maintain clean-room integrity

## Questions?

Not sure if your contribution is clean? **Ask first** via issue or discussion.

We'd rather clarify upfront than reject a PR later.

---

**Remember:** This project's legal status depends on clean-room integrity.
When in doubt, disclose your sources and methods. Transparency protects everyone.

Thank you for contributing responsibly! 🚀
