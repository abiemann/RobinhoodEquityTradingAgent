# TODO — roadmap

## Accessibility / platform validation

- Check whether free ChatGPT users who receive free access to Luna can run the trading agent end to end, including local project-file and shell access, the custom Robinhood MCP connector, Act mode, per-tool approval controls, scheduled tasks, and the broker-result file handoff required by snapshot staging. Document any plan or platform limitations clearly in the quick-start guide.
- Validate formal macOS support before advertising it: add a `macos-latest` CI test and perform one end-to-end dry run on a Mac. The Agent should already work there, but it is not formally supported until both checks pass.
