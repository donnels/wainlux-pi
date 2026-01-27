# Clean Room Statement

## Protocol Reverse-Engineering

All K6 laser engraver protocol documentation and implementation in this repository
was derived through **clean-room reverse-engineering** methods:

### Methods Used
1. **USB packet capture** - Observing data exchange between vendor software and device
2. **Serial monitoring** - Recording commands and responses at 115200 baud
3. **Black-box testing** - Empirical testing of command sequences and parameters
4. **Public documentation** - Analysis of publicly available K3 protocol references
5. **Decompilation for verification** - Ghidra analysis of vendor binaries through LLM and MCP to *verify* empirically observed protocol behavior

### Decompilation Methodology (Verification Only)

**Critical distinction:** Decompiled code was used for **verification**, not **implementation**.

**Process:**
1. **Observe first:** USB captures and device testing revealed protocol structure
2. **Implement independently:** Code written based on observed packet formats and device responses
3. **Verify with decompilation:** Ghidra/ILSpy used to confirm our understanding was correct
4. **Document discrepancies:** Where our observations differed from decompiled code, we tested on hardware

**Example:**
- Observed: Header is 38 bytes, big-endian, depth defaults to 10
- Verified: Decompiled Java confirmed structure and default values
- Implemented: Python code written from documented structure, not translated from Java

**AI-Mediated Analysis:**
Ghidra was accessed via Model Context Protocol (MCP) with an LLM (Claude) acting as intermediary:
- LLM analyzed decompiled code to answer specific protocol questions (e.g., "what are slider min/max values?")
- User received structured answers without directly reading vendor source line-by-line
- This created separation between implementation and vendor code internals
- **Note:** Not equivalent to strict two-team clean-room, but reduces direct code exposure
- User could have read files directly if desired and still could but didn't and won't becasue he couldn't tell a line of java from random noise.

**Not used:**
- Algorithms or business logic from vendor code
- Variable names (Chinese pinyin like `zhang_san`, `li_si_wu` - made-up examples, not actual vendor names) or comments from decompiled code
- Implementation details beyond packet structure
- Code structure or design patterns
- Direct translation of decompiled code

**Note:** Documentation may cite original function names when referencing decompiled source locations (e.g., "found in `vendor_class.foo_bar`" - placeholder example), but implementation uses English names only.

**Legal basis:** Decompilation for interoperability is established as fair use (Sega v. Accolade, Sony v. Connectix)

### Sources NOT Used
- Decompiled vendor source code (not included in this repository)
- Proprietary firmware binaries (not redistributed)
- Vendor SDKs or internal documentation
- Leaked or confidential materials

### Excluded Content
The following are **explicitly excluded** from this repository:
- `backup/wainlux.sw/` - Vendor software and drivers (gitignored)
- `backup/K3_LASER_ENGRAVER_PROTOCOL/` - Third-party K3 reference (gitignored)
- `analysis/java/` - Decompiled artifacts used only for verification (gitignored)
- Vendor binaries (*.exe, *.jar, ROM.bin) - Not redistributed

**Important:** Decompiled code was analyzed but is NOT included in the repository.
All distributed code is original implementation based on documented protocol behavior.

### Implementation Independence
All code in `/docker-wainlux` and `/scripts` was written independently based on
observed protocol behavior, not copied or derived from vendor source code.

### Purpose
This clean-room approach ensures:
- Legal distribution under MIT/CC licenses
- No copyright infringement
- No trade secret misappropriation
- Community can safely use and build upon this work

### Verification
Protocol correctness was validated solely through:
- Device responses (ACK/NAK bytes)
- Actual laser behavior (movement, marking)
- Comparison with captured USB traffic
- Published K3 protocol similarities (not as many as hoped)

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for contamination control policies
and contributor declarations required for pull requests.

If you have questions about the reverse-engineering process or licensing,
please open an issue on GitHub.
