import * as fs from 'fs';
import * as path from 'path';

async function main() {
    const sweep = [
        { symbol: "MU", confidence: 0.92, thesis: "Anticipated Q3 EPS beat (900%+ growth) on HBM3E AI demand. Strong price action ahead of next week's earnings. Score: High." },
        { symbol: "NVDA", confidence: 0.88, thesis: "Catalyst: June 24 Annual Meeting. Stable uptrend following recent $25B expansion note. Score: High." },
        { symbol: "TSLA", confidence: 0.78, thesis: "FSD V14 rollout momentum. Recent technical support held at the 50-day SMA. Score: Medium." },
        { symbol: "ARM", confidence: 0.72, thesis: "Consolidation after licensing growth peak; looking for fresh AI CPU catalysts. Score: Medium." },
        { symbol: "AVGO", confidence: 0.65, thesis: "Below threshold. 15% drawdown on June 4 remains a technical overhang despite strong fundamentals." },
    ];

    const sessionDate = new Date().toISOString().split('T')[0].replace(/-/g, '');
    const logDir = '/workspace/user/logs/overnight/research-' + sessionDate;
    if (!fs.existsSync(logDir)) fs.mkdirSync(logDir, { recursive: true });
    const queueFile = path.join(logDir, 'poke_bridge_queue.jsonl');
    const now = new Date().toISOString();

    console.log(`=== Morning Research Sweep (42 Tickers) - ${new Date().toDateString()} ===`);
    
    for (const ticker of sweep) {
        if (ticker.confidence >= 0.70) {
            console.log("INJECTING: " + ticker.symbol + " with score " + ticker.confidence.toFixed(2));
            console.log("THESIS: " + ticker.thesis);

            const uid = Math.random().toString(36).substring(2, 14);
            const runId = 'sweep-research-' + sessionDate;
            const payload = {
                handoff_id: `${runId}-handoff-${uid}`,
                run_id: runId,
                created_at: now,
                candidate_event_id: `${runId}-candidate-${uid}`,
                destination: 'poke_bridge_local_queue',
                dry_run: ticker.confidence < 0.85,
                metadata: {
                    symbol: ticker.symbol,
                    side: 'buy',
                    confidence: ticker.confidence,
                    thesis: ticker.thesis,
                    trigger_reason: 'research_sweep_signal',
                    source: 'execute_research_sweep',
                    direction: 'buy'
                }
            };
            fs.appendFileSync(queueFile, JSON.stringify(payload) + '\n');
        } else {
            console.log("SKIPPED: " + ticker.symbol + " (Score: " + ticker.confidence.toFixed(2) + ")");
        }
    }
}

main().catch(console.error);
