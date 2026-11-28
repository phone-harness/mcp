# ChatGPT plugin package

The package uploaded at [platform.openai.com/plugins](https://platform.openai.com/plugins) for the
ChatGPT plugin directory. Listing text, icons and review test cases are read-only in the dashboard:
change them here, raise `version` in `plugin.json`, and upload a new ZIP.

    cd chatgpt-plugin && zip -r -X ../phone-harness-plugin.zip plugin.json mcp.json assets

`mcp.json` points at `https://mcp.phone-harness.com/chatgpt`, the endpoint with no reservation tools
and no prices. OpenAI does not let an existing plugin change its MCP URL.

Guide: https://developers.openai.com/plugins/deploy/submission
