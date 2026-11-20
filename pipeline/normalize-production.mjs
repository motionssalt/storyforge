import fs from 'node:fs';
import {normalizeProduction} from '../web/shared/production.mjs';
process.stdout.write(JSON.stringify(normalizeProduction(JSON.parse(fs.readFileSync(0, 'utf8')))));
