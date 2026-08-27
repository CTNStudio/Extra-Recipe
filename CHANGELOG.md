# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/), and this project adheres to [Semantic Versioning](https://semver.org/).

---

## v4.0.5

This version is limited to:
- 57-1.21.2-1.21.3+

### Fixed

- Fixed shaped (ordered) crafting recipes being broken in versions 1.21.2+ (pack_format >= 57), where the `key` field was incorrectly converted into a list of character keys, losing the ingredient mapping and rendering the recipes unusable.
