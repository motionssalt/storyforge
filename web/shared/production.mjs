const styles = ['photo_person', 'photo_object', 'photo_animal', 'photo_scenery', 'cartoon'];
const hash = text => Array.from(text).reduce((h, ch) => Math.imul(h ^ ch.codePointAt(0), 16777619) >>> 0, 2166136261);
const handle = name => String(name ?? '').trim().toLowerCase().replace(/^\/?u\//, '');
const choiceKey = av => JSON.stringify([av?.style, av?.gender, av?.keyword]);
const object = x => x && typeof x === 'object' && !Array.isArray(x);

export function normalizeProduction(input) {
  if (!object(input) || ![undefined, 1, 2, 3].includes(input.schema_version) || !Array.isArray(input.parts)) return { doc: input, notices: [], errors: [] };
  const doc = JSON.parse(JSON.stringify(input));
  const notices = [], errors = [];
  const missingAvatar = doc.parts.map(part => !Object.hasOwn(part?.post ?? {}, 'avatar'));
  const missingName = doc.parts.map(part => !handle(part?.post?.username));
  doc.parts.forEach((part, i) => {
    if (!object(part) || part.post || !Array.isArray(part.cards) || !part.cards.length) return;
    const c = part.cards.find(item => ['post', 'branded'].includes(item?.card?.type))?.card ?? part.cards[0]?.card ?? {};
    part.post = Object.fromEntries(Object.entries(c).filter(([key]) => ['subreddit','username','age','title','upvotes','comments','comments_count','shares','awards','flair','nsfw','verified','karma','follower_count','theme','avatar','show_footer'].includes(key)));
    part.post.username ??= c.handle;
    missingName[i] = !handle(part.post.username);
    missingAvatar[i] = !Object.hasOwn(part.post, 'avatar');
    if (!part.description) {
      const desc = String(part.story_so_far || part.post.title || part.title || '').trim();
      part.description = desc.length >= 10 ? desc : `${desc} — full story narrated in this clip.`.trim();
    }
    delete part.cards;
    notices.push(`Part ${i + 1}: converted legacy cards to a persistent post.`);
  });
  const identityName = doc.parts.find(p => handle(p?.post?.username))?.post.username;
  const identityAvatar = doc.parts.find((p, i) => !missingAvatar[i])?.post.avatar;
  if (doc.schema_version !== 3) notices.push('Upgraded production data to schema version 3.');
  const legacy = doc.schema_version !== 3;
  doc.schema_version = 3;
  doc.parts.forEach((part, i) => {
    if (!object(part)) return;
    if (part.post === undefined) part.post = {};
    const p = part.post;
    if (!object(p)) return;
    if (Object.hasOwn(p, 'comments_count')) {
      p.comments ??= p.comments_count;
      delete p.comments_count;
      notices.push(`Part ${i + 1}: renamed comments_count to comments.`);
    }
    const auto = [];
    const fill = (key, value) => {
      if (!Object.hasOwn(p, key) || p[key] === undefined) { p[key] = value; auto.push(key); }
    };
    const storySeed = String(doc.series_title ?? part.title ?? '');
    fill('username', identityName ?? `u/throwaway_${hash(storySeed).toString(36)}`);
    if (i && object(doc.parts[0]?.post) && handle(p.username) !== handle(doc.parts[0].post.username)) {
      if (!legacy) errors.push('Part 2 post.username must match Part 1.');
      else { p.username = doc.parts[0].post.username; notices.push('Part 2: reused the Part 1 username for continuity.'); }
    }
    const seed = handle(p.username) || storySeed;
    const roll = (key, min, max) => min + hash(`${seed}|${key}`) % (max - min + 1);
    const votes = roll(`votes${i}`, 72, 15400);
    fill('subreddit', ['r/stories', 'r/lifeupdates', 'r/storytime', 'r/TrueOffMyChest'][roll('sub',0,3)]);
    fill('age', `${roll(`age${i}`,1,22)}h`);
    fill('title', part.title ?? doc.series_title ?? `Story ${hash(seed).toString(36)}`);
    fill('upvotes', votes);
    fill('comments', Math.round(votes * roll('comment-ratio', 3, 17) / 100));
    fill('shares', Math.round(votes * roll('share-ratio', 1, 9) / 100));
    fill('awards', roll('awards', 0, 4) === 0 ? [{type:'helpful',count:roll('award-count',1,5)}] : []);
    if (!Object.hasOwn(p, 'karma') && !Object.hasOwn(p, 'follower_count')) fill('karma', roll('karma', 135, 32500));
    fill('theme', roll('theme', 0, 3) ? 'dark' : 'light');
    // Do not infer an NSFW classification from a random number.
    fill('nsfw', false);
    fill('verified', roll('verified', 0, 99) === 0);
    fill('avatar', identityAvatar ?? {style: styles[roll('avatar',0,styles.length-1)]});
    if (i && object(doc.parts[0]?.post) && choiceKey(p.avatar) !== choiceKey(doc.parts[0].post.avatar)) {
      if (!legacy) errors.push('Part 2 post.avatar must match Part 1.');
      else { p.avatar = doc.parts[0].post.avatar; notices.push('Part 2: reused the Part 1 avatar for continuity.'); }
    }
    if (auto.length) notices.push(`Part ${i + 1} auto-filled: ${auto.join(', ')}.`);
  });
  return { doc, notices, errors };
}
