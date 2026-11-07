// A Keiki (onkeiki.com) iMessage agent that drives each person's own Phone
// Harness phone. Every person signs in to their own Phone Harness account the
// first time; the deployer's account is never used.
import { create } from '@onkeiki/agents'
import { phonePrompt } from './prompt.js'

const env = (name: string): string | undefined => Bun.env[name]
const apiKey = env('KEIKI_API_KEY')

if (!apiKey) {
  throw new Error('Missing KEIKI_API_KEY. Set it in examples/keiki/.env before syncing.')
}

const kit = create({
  apiKey,
  name: env('AGENT_NAME') ?? 'Phone',
  agent: {
    model: env('AGENT_MODEL') ?? 'openai/gpt-6.1-sol',
    maxSteps: 40,
    historyLimit: 60,
    reasoningEffort: 'low',
    systemPrompt: () => phonePrompt,
    tools: [],
    // Code mode runs in the sandbox: it downloads screenshot links and sends
    // them with send_file.
    sandbox: { enabled: true, persistent: true },
    mcp: {
      enabled: true,
      userConnections: [{
        kind: 'mcp',
        // MCP connection names must match ^[a-z][a-z0-9_]*$.
        name: 'phone_harness',
        vendor: 'phone-harness',
        description:
          "Use each person's own Phone Harness cloud phone: start their saved Android phone, see and drive the screen, " +
          'send them screenshots, and hand the phone to them for passwords, codes and CAPTCHAs.',
        aliases: ['phone harness', 'phone-harness', 'my phone', 'cloud phone'],
        capabilities: ['phone.start', 'phone.drive', 'phone.screenshot', 'phone.handoff'],
        endpoint: env('PHONE_HARNESS_MCP_URL') ?? 'https://mcp.phone-harness.com/mcp',
        // No `oauth` block: the server supports dynamic client registration,
        // and its sign-in asks for the phone:drive scope by itself.
      }],
    },
    steering: { debounceWindow: '2 seconds', midTurnStrategy: 'interrupt', maxBatch: 5 },
    media: {
      audio: { action: 'transcribe' },
      image: { action: 'describe' },
      video: { action: 'describe' },
      document: { action: 'extract' },
      fallback: '[Unreadable attachment]',
    },
    guards: true,
    promptGuard: true,
  },
})

await kit.sync()
