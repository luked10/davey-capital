import * as fs from 'fs';
import * as path from 'path';

async function inject() {
    const sessionDate = new Date().toISOString().split('T')[0].replace(/-/g, '');
    const logDir = '/workspace/user/logs/overnight/research-' + sessionDate;
    
    if (!fs.existsSync(logDir)) {
        fs.mkdirSync(logDir, { recursive: true });
    }
    
    const queueFile = path.join(logDir, 'poke_bridge_queue.jsonl');
    const now = new Date().toISOString();
    
    const candidates = [
        {
            symbol: 'NVDA',
            thesis: '$25B bond sale expansion, incredible margins, June 24 annual meeting catalyst',
            confidence: 0.90,
            direction: 'buy'
        },
        {
            symbol: 'AMD',
            thesis: 'pullback to short-term support, Meta/Citi tailwinds, high long-term AI upside despite current 5% correction',
            confidence: 0.75,
            direction: 'buy'
        }
    ];
    
    for (const c of candidates) {
        const uid = Math.random().toString(36).substring(2, 14);
        const runId = 'poke-research-' + sessionDate;
        const handoffId = `${runId}-handoff-${uid}`;
        const eventId = `${runId}-candidate-${uid}`;
        
        const payload = {
            handoff_id: handoffId,
            run_id: runId,
            created_at: now,
            candidate_event_id: eventId,
            destination: 'poke_bridge_local_queue',
            dry_run: c.confidence < 0.85,
            metadata: {
                symbol: c.symbol,
                side: c.direction,
                confidence: c.confidence,
                thesis: c.thesis,
                trigger_reason: 'manual_injection',
                source: 'poke_research',
                direction: c.direction
            }
        };
        
        fs.appendFileSync(queueFile, JSON.stringify(payload) + '\n');
        console.log(`Injected ${c.symbol} with handoff_id: ${handoffId} (dry_run: ${payload.dry_run})`);
    }
}

inject().catch(console.error);
