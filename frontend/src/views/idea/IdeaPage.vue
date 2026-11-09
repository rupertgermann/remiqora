<script setup lang="ts">
// "From an idea": one field for the idea, song or instrumental, an engine, and the prompt
// written for it in that engine's own format:
// - ACE-Step: style caption, lyrics, BPM, key, length; by the prompt assistant, or by ACE-Step's own language model
//   when the engine runs and that is picked (faster, but it strays from the idea);
// - YuE2: English style guidance, with sectioned lyrics for songs;
// - Stable Audio 3 (instrumentals only): its own tag style, by the assistant with Stable Audio's own instructions.
// The prompt is shown as editable fields; "Generate" starts the engine if needed and sends the request its own page
// sends, then opens that page, where the track lands in the feed.
import { computed, onMounted, ref, watch } from 'vue'
import type { Component } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRouter } from 'vue-router'
import { storeToRefs } from 'pinia'
import * as aceApi from '../../api/aceStep'
import type { GenerateMusicRequest } from '../../api/aceStep'
import { PromptCancelled, writePromptLive } from '../../api/assistant'
import type { AssistantTask } from '../../api/assistant'
import type { Sa3ModelId } from '../../api/stableAudio'
import { MODEL_LABELS } from '../../composables/useModelSwitch'
import { useAceStepStore } from '../../stores/aceStep'
import { useEnginesStore } from '../../stores/engines'
import { useFeaturesStore } from '../../stores/features'
import { useOrchestratorStore } from '../../stores/orchestrator'
import { useYue2Store } from '../../stores/yue2'
import { useIdeaStore } from '../../stores/idea'
import type { IdeaDraft as Draft, IdeaEngine as Engine, IdeaWriter as Writer } from '../../stores/idea'
import { useStableAudioStore } from '../../stores/stableAudio'
import { englishLanguageName, guessVocalLanguage, VOCAL_LANGUAGES, withLanguageTag } from '../../utils/vocalLanguages'
import ChipGroup from '../../components/shared/ChipGroup.vue'
import PromptProgress from '../../components/shared/PromptProgress.vue'
import HelpIconButton from '../../components/shared/HelpIconButton.vue'
import IdeaGuide from '../../components/idea/IdeaGuide.vue'
import MicIcon from '../../components/shared/icons/MicIcon.vue'
import PulseIcon from '../../components/shared/icons/PulseIcon.vue'
import SparklesIcon from '../../components/shared/icons/SparklesIcon.vue'
import WaveIcon from '../../components/shared/icons/WaveIcon.vue'

const { t } = useI18n()
const router = useRouter()
const orchestrator = useOrchestratorStore()
const ace = useAceStepStore()
const yue2 = useYue2Store()
const sa3 = useStableAudioStore()
const engines = useEnginesStore()
const features = useFeaturesStore()

// ---- the idea: in a store, so leaving for an engine's page and coming back keeps it, and the browser keeps it ----
const ideaStore = useIdeaStore()
const { idea, kind, language, writerPref, engine, draft, variants, precision, sa3Model, writing, error, progress, stopping } = storeToRefs(ideaStore)
const instrumental = computed(() => kind.value === 'instrumental')
const effectiveLanguage = computed(() => language.value || (idea.value.trim() ? guessVocalLanguage(idea.value) : 'en'))
const kindOptions = computed(() => [
  { value: 'song', label: t('idea.song') },
  { value: 'instrumental', label: t('idea.instrumental') },
])

// ---- the engine ----
const ENGINES: { id: Engine; model: 'ace_step' | 'yue2' | 'stable_audio'; route: string; icon: Component }[] = [
  { id: 'ace', model: 'ace_step', route: 'ace-step', icon: WaveIcon },
  { id: 'yue2', model: 'yue2', route: 'yue2', icon: MicIcon },
  { id: 'sa3', model: 'stable_audio', route: 'stable-audio', icon: PulseIcon },
]
/** Stable Audio's checkpoints for an idea, the better first for each kind: medium makes music and sounds,
 *  small-music only music, small-sfx only sounds (the base ones want CFG and 50 steps, so they stay on its page). */
const SA3_MODELS: { id: Sa3ModelId; max: number; kinds: ('music' | 'sfx')[] }[] = [
  { id: 'medium', max: 380, kinds: ['music', 'sfx'] },
  { id: 'small-music', max: 120, kinds: ['music'] },
  { id: 'small-sfx', max: 120, kinds: ['sfx'] },
]
const sa3Installed = computed(() => SA3_MODELS.filter((m) => features.features.stable_audio_models.includes(m.id)))
/** Music or a sound: as the prompt says, music before there is one. */
const sa3Kind = computed<'music' | 'sfx'>(() => (draft.value?.engine === 'sa3' && draft.value.sa3Kind) || 'music')
/** For sounds small-sfx first, it was made for them. */
const sa3Models = computed(() => {
  const list = sa3Installed.value.filter((m) => m.kinds.includes(sa3Kind.value))
  return sa3Kind.value === 'sfx' ? [...list].sort((a, b) => Number(b.id === 'small-sfx') - Number(a.id === 'small-sfx')) : list
})
const engineInfo = computed(() => ENGINES.find((e) => e.id === engine.value)!)
function engineAvailable(id: Engine): boolean {
  if (id === 'yue2') return features.has('yue2')
  if (id === 'sa3') return features.has('stableAudio') && instrumental.value && sa3Installed.value.length > 0
  return true
}
function unavailableReason(id: Engine): string {
  if (id === 'sa3' && !instrumental.value) return t('idea.sa3OnlyInstrumentals')
  if (id === 'sa3' && features.has('stableAudio')) return t('idea.sa3NoMusicModel')
  return t('idea.notInstalled')
}
function engineRunning(id: Engine): boolean {
  const model = ENGINES.find((e) => e.id === id)!.model
  return orchestrator.activeModel === model && orchestrator.statuses[model]?.status === 'running'
}
watch(instrumental, (instr) => {
  if (draft.value?.engine === 'yue2') draft.value = null
  if (!instr && engine.value === 'sa3') engine.value = 'ace'
})
watch(engine, (now) => { if (draft.value && draft.value.engine !== now) draft.value = null })
const yue2Precisions = computed(() => features.features.yue2_precisions)

// ---- who writes the prompt ----
const aceRunning = computed(() => engineRunning('ace'))
/** A downloaded model is enough: the assistant starts on the first request. */
const assistantReady = computed(() => !!engines.assistant?.ready && !!engines.assistant?.models.some((m) => m.installed))
/** YuE2 has no writer of its own. For ACE-Step: the picked writer when it can write now, else the other one. */
const writer = computed<Writer | null>(() => {
  if (engine.value !== 'ace') return assistantReady.value ? 'assistant' : null
  const can = { assistant: assistantReady.value, ace: aceRunning.value }
  if (can[writerPref.value]) return writerPref.value
  return can.assistant ? 'assistant' : can.ace ? 'ace' : null
})
const writerOptions = computed(() => [
  { value: 'assistant', label: t('idea.writerAssistant'), disabled: !assistantReady.value },
  { value: 'ace', label: t('idea.writerAce'), disabled: !aceRunning.value },
])
/** The assistant set to the GPU: it unloads a running engine to write, and leaves when Generate starts the engine. */
const assistantOnGpu = computed(() => engines.assistant?.settings.device === 'cuda')
/** An engine holds the GPU and the assistant is not set to take it: it writes on the CPU, so it takes longer. */
const assistantOnCpu = computed(() => !!orchestrator.activeModel && !assistantOnGpu.value)

// ---- the prompt, editable ----
async function byAssistant(task: AssistantTask, extra: { language?: string; kind?: string }) {
  stopping.value = false
  const run = writePromptLive(task, idea.value.trim(), extra, (p) => { progress.value = p }, {
    unloadsEngine: assistantOnGpu.value && !!orchestrator.activeModel,
  })
  ideaStore.setStop(run.cancel)
  try {
    return (await run.result).fields
  } finally {
    ideaStore.setStop(null)
    progress.value = null
  }
}
const stopWriting = () => ideaStore.stop()

async function write() {
  const text = idea.value.trim()
  if (!text) {
    error.value = t('idea.needIdea')
    return
  }
  if (!writer.value) {
    error.value = t('idea.noWriter')
    return
  }
  writing.value = true
  error.value = null
  const t0 = performance.now()
  const seconds = () => (performance.now() - t0) / 1000
  const base = { bpm: null, keyscale: '', duration: 120, timesignature: '', language: instrumental.value ? '' : effectiveLanguage.value }
  try {
    if (engine.value === 'sa3') {
      // Stable Audio's own classifier reads the idea first: "rain noise" is a sound, not a track, and gets its
      // SFX instructions (a solo instrument and a one-shot get theirs too)
      const f = await byAssistant('sa3', {})
      const kindOf: 'music' | 'sfx' = f.category === 'sfx' || f.category === 'one_shot' ? 'sfx' : 'music'
      const fits = sa3Installed.value.filter((m) => m.kinds.includes(kindOf))
      const max = (fits.find((m) => m.id === sa3Model.value) ?? fits[0])?.max ?? 120
      const dur = typeof f.duration === 'number' ? f.duration : kindOf === 'sfx' ? 10 : 60
      draft.value = {
        ...base, engine: 'sa3', caption: String(f.prompt ?? ''), lyrics: '', sa3Kind: kindOf,
        bpm: typeof f.bpm === 'number' ? f.bpm : null,
        duration: Math.max(kindOf === 'sfx' ? 0.5 : 5, Math.min(max, Math.round(dur))), writtenBy: 'assistant', seconds: seconds(),
      }
    } else if (engine.value === 'yue2') {
      const isInstrumental = instrumental.value
      const songLanguage = effectiveLanguage.value
      const f = await byAssistant(isInstrumental ? 'ace-instrumental' : 'yue2-song', isInstrumental ? {} : { language: songLanguage })
      if (isInstrumental !== instrumental.value || engine.value !== 'yue2') return
      const lang = typeof f.language === 'string' && f.language ? f.language : englishLanguageName(songLanguage)
      draft.value = {
        ...base, engine: 'yue2',
        caption: isInstrumental ? String(f.caption ?? '') : withLanguageTag(String(f.style ?? ''), lang),
        lyrics: isInstrumental ? '' : String(f.lyrics ?? ''),
        writtenBy: 'assistant', seconds: seconds(),
      }
    } else if (writer.value === 'ace') {
      const s = await aceApi.createSample(text, instrumental.value, instrumental.value ? undefined : effectiveLanguage.value)
      draft.value = {
        engine: 'ace',
        caption: s.caption,
        lyrics: instrumental.value ? '' : s.lyrics,
        bpm: s.bpm ?? null,
        keyscale: s.keyscale || '',
        duration: Math.round(s.duration || 120),
        timesignature: s.timesignature || '',
        language: instrumental.value ? '' : s.vocal_language && s.vocal_language !== 'unknown' ? s.vocal_language : effectiveLanguage.value,
        writtenBy: 'ace',
        seconds: seconds(),
      }
    } else {
      const f = await byAssistant(instrumental.value ? 'ace-instrumental' : 'ace-song', { language: effectiveLanguage.value })
      draft.value = {
        ...base,
        engine: 'ace',
        caption: String(f.caption ?? ''),
        lyrics: instrumental.value ? '' : String(f.lyrics ?? ''),
        bpm: typeof f.bpm === 'number' ? f.bpm : null,
        keyscale: String(f.keyscale ?? ''),
        duration: typeof f.duration === 'number' ? f.duration : 120,
        writtenBy: 'assistant',
        seconds: seconds(),
      }
    }
    if (writer.value === 'assistant') {
      void engines.load()
      // on the GPU the assistant may have unloaded the running engine
      if (assistantOnGpu.value) void orchestrator.refresh()
    }
  } catch (e) {
    // stopped by hand: no error, the previous prompt (if any) stays
    if (!(e instanceof PromptCancelled)) error.value = e instanceof Error ? e.message : String(e)
  } finally {
    writing.value = false
  }
}

const durationLabel = computed(() => {
  const d = draft.value?.duration ?? 0
  return `${Math.floor(d / 60)}:${String(Math.round(d % 60)).padStart(2, '0')}`
})

// ---- generate ----
const variantOptions = [1, 2, 4].map((n) => ({ value: n, label: String(n) }))
watch(sa3Models, (list) => { if (list.length && !list.some((m) => m.id === sa3Model.value)) sa3Model.value = list[0].id }, { immediate: true })
// music and sounds have their own best checkpoint: a change of kind picks it
watch(sa3Kind, () => { if (sa3Models.value.length) sa3Model.value = sa3Models.value[0].id })
const sa3KindOptions = computed(() => [
  { value: 'music', label: t('idea.sa3Music') },
  { value: 'sfx', label: t('idea.sa3Sound') },
])
/** The service tag the assistant put first, per kind (Stable Audio's TRACK_TYPE_PREFIXES). */
const SA3_PREFIX = { music: 'TrackType: Music, VocalType: Instrumental, ', sfx: 'TrackType: SFX, ' } as const
function setSa3Kind(value: unknown) {
  const next = value === 'sfx' ? 'sfx' : 'music'
  const d = draft.value
  if (!d || d.engine !== 'sa3' || (d.sa3Kind ?? 'music') === next) return
  const from = SA3_PREFIX[d.sa3Kind ?? 'music']
  if (d.caption.startsWith(from)) d.caption = SA3_PREFIX[next] + d.caption.slice(from.length)
  d.sa3Kind = next
  if (next === 'music' && d.duration < 5) d.duration = 30
}
const sa3Max = computed(() => sa3Models.value.find((m) => m.id === sa3Model.value)?.max ?? 120)
watch(yue2Precisions, (list) => { if (list.length && !list.includes(precision.value)) precision.value = list[0] }, { immediate: true })
const starting = ref(false)

function aceRequest(d: Draft): GenerateMusicRequest {
  const req: GenerateMusicRequest = {
    prompt: d.caption.trim(),
    lyrics: instrumental.value ? '' : d.lyrics,
    audio_duration: Math.max(10, Math.min(600, Math.round(d.duration))),
    batch_size: variants.value,
    audio_format: 'mp3',
    use_random_seed: true,
  }
  if (d.bpm) req.bpm = Math.round(d.bpm)
  if (d.keyscale.trim()) req.key_scale = d.keyscale.trim()
  if (d.timesignature) req.time_signature = d.timesignature
  if (!instrumental.value && d.language) req.vocal_language = d.language
  return req
}

/** The idea as the track's title, cut at a word near 60 characters (a cut at the first comma left "a song about"). */
function titleOf(text: string): string {
  const one = text.trim().replace(/\s+/g, ' ')
  if (one.length <= 60) return one
  const cut = one.slice(0, 60)
  const space = cut.lastIndexOf(' ')
  return `${(space > 30 ? cut.slice(0, space) : cut).replace(/[,.;:!?\s]+$/, '')}…`
}

async function generate() {
  const d = draft.value
  const isInstrumental = instrumental.value
  if (!d || !d.caption.trim()) {
    error.value = t('idea.needCaption')
    return
  }
  if (d.engine === 'yue2' && !isInstrumental && !d.lyrics.trim()) {
    error.value = t('idea.needLyrics')
    return
  }
  error.value = null
  starting.value = true
  const info = ENGINES.find((e) => e.id === d.engine)!
  try {
    if (!engineRunning(d.engine)) await orchestrator.switchModel(info.model)
    if (d.engine === 'sa3') {
      await sa3.generate({
        kind: d.sa3Kind ?? 'music', model: sa3Model.value, prompt: d.caption.trim(), caption: idea.value.trim(), autoTags: false,
        negativePrompt: '', duration: Math.max(d.sa3Kind === 'sfx' ? 0.5 : 5, Math.min(sa3Max.value, d.duration)),
        steps: 8, cfgScale: 1, seed: -1, count: variants.value, title: titleOf(idea.value || d.caption),
      })
    } else if (d.engine === 'yue2') {
      await yue2.generateBatch({
        lyrics: d.lyrics.trim(),
        instrumental: isInstrumental,
        precision: precision.value,
        baseSeed: Math.floor(Math.random() * 2147483647),
        randomSeed: true,
        batchSize: variants.value,
        options: { style: d.caption.trim(), cot: 'off' },
        title: titleOf(idea.value || d.caption),
      })
    } else {
      await ace.submit(aceRequest(d), null, titleOf(idea.value || d.caption))
    }
    // the job is sent and the track lands in the engine's feed: the form starts clean for the next idea
    ideaStore.reset()
    await router.push({ name: info.route })
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    starting.value = false
  }
}

/** The engine's own form with this prompt in it, for the settings this page leaves out. */
async function openInForm() {
  const d = draft.value
  if (!d) return
  if (d.engine === 'yue2') {
    yue2.requestInsertParams({ lyrics: d.lyrics, style: d.caption, precision: precision.value, instrumental: instrumental.value })
    await router.push({ name: 'yue2' })
    return
  }
  if (d.engine === 'sa3') {
    sa3.requestInsertParams({ kind: d.sa3Kind ?? 'music', model: sa3Model.value, prompt: d.caption, duration: Math.min(sa3Max.value, Math.round(d.duration)) })
    await router.push({ name: 'stable-audio' })
    return
  }
  const req = aceRequest(d)
  ace.requestInsertParams({
    prompt: req.prompt,
    lyrics: req.lyrics,
    instrumental: instrumental.value,
    duration: req.audio_duration,
    bpm: req.bpm ?? null,
    key_scale: req.key_scale ?? '',
    time_signature: req.time_signature ?? '',
    vocal_language: req.vocal_language ?? '',
  })
  await router.push({ name: 'ace-step' })
}

async function startAce() {
  try {
    await orchestrator.switchModel('ace_step')
  } catch {
    // orchestrator.switchError is shown in the header
  }
}

onMounted(() => {
  void engines.load()
})
const guideOpen = ref(false)
</script>

<template>
  <div class="mx-auto max-w-3xl">
    <header>
      <h1 class="flex items-center gap-2 text-2xl font-semibold text-text">
        {{ t('idea.title') }}
        <HelpIconButton class="mt-1" @click="guideOpen = true" />
      </h1>
      <p class="mt-1 text-sm leading-relaxed text-text-dim">{{ t('idea.lead') }}</p>
    </header>

    <!-- the idea -->
    <section class="mt-5 rounded-xl border border-border bg-panel p-4">
      <div class="flex flex-wrap items-center justify-between gap-3">
        <ChipGroup v-model="kind" :options="kindOptions" />
        <label v-if="!instrumental" class="flex items-center gap-2 text-xs text-text-dim">
          {{ t('idea.language') }}
          <select v-model="language" class="rounded-lg border border-border bg-panel-2 px-2 py-1.5 text-sm text-text focus:border-accent1 focus:outline-none">
            <option value="">{{ t('idea.languageAuto', { lang: VOCAL_LANGUAGES.find((l) => l.code === effectiveLanguage)?.label ?? effectiveLanguage }) }}</option>
            <option v-for="l in VOCAL_LANGUAGES" :key="l.code" :value="l.code">{{ l.label }}</option>
          </select>
        </label>
      </div>
      <textarea
        v-model="idea"
        rows="3"
        :aria-label="t('idea.ideaLabel')"
        :placeholder="t(instrumental ? 'idea.placeholderInstrumental' : 'idea.placeholderSong')"
        class="mt-3 w-full resize-y rounded-lg border border-border bg-panel-2 px-3 py-2.5 text-base leading-relaxed text-text placeholder:text-text-dim/70 focus:border-accent1 focus:outline-none"
        @keydown.ctrl.enter="write"
      ></textarea>

      <!-- the engine -->
      <div class="mt-3 flex flex-wrap items-center gap-x-2 gap-y-2" role="radiogroup" :aria-label="t('idea.engine')">
        <span class="me-1 text-xs text-text-dim">{{ t('idea.engine') }}</span>
        <button
          v-for="e in ENGINES"
          :key="e.id"
          type="button"
          role="radio"
          :aria-checked="engine === e.id"
          :disabled="!engineAvailable(e.id)"
          :title="!engineAvailable(e.id) ? unavailableReason(e.id) : t(e.id === 'yue2' && instrumental ? 'yueGen.instrumentalHint' : `idea.engineHint.${e.id}`)"
          class="inline-flex items-center gap-2 rounded-lg border px-3 py-1.5 text-sm transition-colors disabled:cursor-not-allowed disabled:opacity-40"
          :class="engine === e.id ? 'border-accent1/50 bg-accent1/10 text-text' : 'border-border text-text-dim hover:border-text-dim hover:text-text'"
          @click="engine = e.id"
        >
          <component :is="e.icon" class="h-4 w-4" :class="engine === e.id && 'text-accent1'" />{{ MODEL_LABELS[e.model] }}
          <span class="h-1.5 w-1.5 rounded-full" :class="engineRunning(e.id) ? 'bg-status-done' : 'bg-text-dim/50'"></span>
        </button>
      </div>
      <div v-if="engine === 'ace' && (assistantReady || aceRunning)" class="mt-3 flex flex-wrap items-center gap-2 text-xs text-text-dim">
        {{ t('idea.writer') }}
        <ChipGroup v-model="writerPref" small :options="writerOptions" />
      </div>

      <!-- the footer: what will happen on the left, the actions on the right, on one line under a rule. Both buttons
           are always there and always as wide, so nothing shifts while typing, writing or after a reset. -->
      <div class="mt-4 flex flex-wrap items-center justify-between gap-x-4 gap-y-3 border-t border-border/60 pt-3">
        <div class="min-w-[15rem] flex-1 space-y-1 text-xs text-text-dim">
          <p>{{ t(engine === 'yue2' && instrumental ? 'yueGen.instrumentalHint' : `idea.engineHint.${engine}`) }}</p>
          <p class="leading-relaxed" :class="writer ? 'text-text-dim' : 'text-status-queued'">
            <template v-if="writer === 'ace'">{{ t('idea.byAce') }}</template>
            <template v-else-if="writer === 'assistant'">{{ assistantOnGpu ? t('idea.byAssistantGpu', { engine: MODEL_LABELS[engineInfo.model] }) : t(assistantOnCpu ? (instrumental ? 'idea.byAssistantCpuInstrumental' : 'idea.byAssistantCpu') : (instrumental ? 'idea.byAssistantInstrumental' : 'idea.byAssistant')) }}</template>
            <template v-else-if="engine !== 'ace'">
              {{ t('idea.noWriterAssistant') }} <RouterLink to="/models#assistant" class="text-accent1 underline">{{ t('idea.setupAssistant') }}</RouterLink>
            </template>
            <template v-else>
              {{ t('idea.noWriter') }}
              <button type="button" class="text-accent1 underline disabled:opacity-50" :disabled="orchestrator.switching" @click="startAce">{{ t('idea.startAce') }}</button>
              · <RouterLink to="/models#assistant" class="text-accent1 underline">{{ t('idea.setupAssistant') }}</RouterLink>
            </template>
            <span v-if="writing && writer === 'ace'" class="ms-1">{{ t('idea.writingAce') }}</span>
          </p>
        </div>
        <div class="ms-auto flex shrink-0 items-center gap-2">
          <button
            type="button"
            class="rounded-lg border border-border px-3 py-2 text-sm text-text-dim transition-colors hover:border-text-dim hover:text-text disabled:pointer-events-none disabled:opacity-40"
            :title="t('idea.resetTitle')"
            :disabled="writing || (!idea.trim() && !draft)"
            @click="ideaStore.reset()"
          >
            {{ t('idea.reset') }}
          </button>
          <button
            type="button"
            class="accent-gradient inline-flex items-center gap-1.5 rounded-lg px-4 py-2 text-sm font-semibold text-white disabled:opacity-50"
            :disabled="writing || !idea.trim() || !writer"
            @click="write"
          >
            <SparklesIcon class="h-4 w-4" />
            <!-- the three labels in one cell: the button keeps the width of the longest -->
            <span class="grid">
              <span
                v-for="k in ['idea.write', 'idea.rewrite', 'idea.writing']"
                :key="k"
                class="col-start-1 row-start-1 text-center"
                :class="k === (writing ? 'idea.writing' : draft ? 'idea.rewrite' : 'idea.write') ? '' : 'invisible'"
                :aria-hidden="k === (writing ? 'idea.writing' : draft ? 'idea.rewrite' : 'idea.write') ? undefined : 'true'"
              >{{ t(k) }}</span>
            </span>
          </button>
        </div>
      </div>
      <PromptProgress v-if="writing && progress" :progress="progress" :stopping="stopping" class="mt-3" @stop="stopWriting" />
      <p v-if="error" class="mt-3 rounded-lg bg-status-failed/10 p-3 text-xs text-status-failed" role="alert">{{ error }}</p>
    </section>

    <!-- the prompt -->
    <section v-if="draft" class="mt-4 overflow-hidden rounded-xl border border-border bg-panel">
      <div class="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-border bg-panel-2/60 px-4 py-2.5">
        <h2 class="text-sm font-semibold text-text">{{ t('idea.promptTitle', { engine: MODEL_LABELS[engineInfo.model] }) }}</h2>
        <span class="text-xs text-text-dim">{{ t(draft.writtenBy === 'ace' ? 'idea.writtenByAce' : 'idea.writtenByAssistant', { s: draft.seconds.toFixed(1) }) }}</span>
      </div>
      <div class="space-y-4 p-4">
        <label class="flex flex-col gap-1 text-xs">
          <span class="font-medium text-text">{{ t(draft.engine === 'yue2' ? 'idea.styleTags' : draft.engine === 'sa3' ? 'idea.sa3Prompt' : 'idea.caption') }}</span>
          <textarea v-model="draft.caption" :rows="draft.engine === 'sa3' ? 4 : 2" class="resize-y rounded-lg border border-border bg-panel-2 px-3 py-2 text-sm leading-relaxed text-text focus:border-accent1 focus:outline-none"></textarea>
          <span v-if="draft.engine === 'yue2' && !instrumental" class="leading-relaxed text-text-dim">{{ t('idea.styleTagsHint') }}</span>
          <span v-else-if="draft.engine === 'sa3'" class="leading-relaxed text-text-dim">{{ t('idea.sa3PromptHint') }}</span>
        </label>
        <label v-if="!instrumental" class="flex flex-col gap-1 text-xs">
          <span class="font-medium text-text">{{ t('idea.lyrics') }}</span>
          <textarea v-model="draft.lyrics" rows="12" class="resize-y rounded-lg border border-border bg-panel-2 px-3 py-2 font-mono text-[13px] leading-relaxed text-text focus:border-accent1 focus:outline-none"></textarea>
        </label>

        <!-- ACE-Step: tempo, key, length, language -->
        <div v-if="draft.engine === 'ace'" class="grid gap-3 sm:grid-cols-4">
          <label class="flex flex-col gap-1 text-xs">
            <span class="font-medium text-text">{{ t('idea.bpm') }}</span>
            <input v-model.number="draft.bpm" type="number" min="30" max="300" class="rounded-lg border border-border bg-panel-2 px-2.5 py-2 text-sm text-text focus:border-accent1 focus:outline-none" />
          </label>
          <label class="flex flex-col gap-1 text-xs">
            <span class="font-medium text-text">{{ t('idea.key') }}</span>
            <input v-model="draft.keyscale" type="text" placeholder="A minor" class="rounded-lg border border-border bg-panel-2 px-2.5 py-2 text-sm text-text focus:border-accent1 focus:outline-none" />
          </label>
          <label class="flex flex-col gap-1 text-xs">
            <span class="font-medium text-text">{{ t('idea.duration', { label: durationLabel }) }}</span>
            <input v-model.number="draft.duration" type="number" min="10" max="600" step="5" class="rounded-lg border border-border bg-panel-2 px-2.5 py-2 text-sm text-text focus:border-accent1 focus:outline-none" />
          </label>
          <label v-if="!instrumental" class="flex flex-col gap-1 text-xs">
            <span class="font-medium text-text">{{ t('idea.vocalLanguage') }}</span>
            <select v-model="draft.language" class="rounded-lg border border-border bg-panel-2 px-2 py-2 text-sm text-text focus:border-accent1 focus:outline-none">
              <option v-for="l in VOCAL_LANGUAGES" :key="l.code" :value="l.code">{{ l.label }}</option>
            </select>
          </label>
        </div>
        <!-- Stable Audio 3: the checkpoint and the length (its own limit per checkpoint) -->
        <div v-else-if="draft.engine === 'sa3'" class="grid gap-3 sm:grid-cols-3">
          <!-- music or a sound, as the classifier read the idea; switching it swaps the prompt's service tag -->
          <div class="flex flex-col gap-1 text-xs">
            <span class="font-medium text-text">{{ t('idea.sa3Kind') }}</span>
            <ChipGroup :model-value="sa3Kind" small :options="sa3KindOptions" @update:model-value="setSa3Kind" />
          </div>
          <label class="flex flex-col gap-1 text-xs">
            <span class="font-medium text-text">{{ t('idea.sa3Model') }}</span>
            <select v-model="sa3Model" class="rounded-lg border border-border bg-panel-2 px-2.5 py-2 text-sm text-text focus:border-accent1 focus:outline-none">
              <option v-for="m in sa3Models" :key="m.id" :value="m.id">{{ m.id }}</option>
            </select>
          </label>
          <label class="flex flex-col gap-1 text-xs">
            <span class="font-medium text-text">{{ t('idea.duration', { label: durationLabel }) }}</span>
            <input v-model.number="draft.duration" type="number" :min="sa3Kind === 'sfx' ? 0.5 : 5" :max="sa3Max" :step="sa3Kind === 'sfx' ? 0.5 : 5" class="rounded-lg border border-border bg-panel-2 px-2.5 py-2 text-sm text-text focus:border-accent1 focus:outline-none" />
            <span class="text-text-dim">{{ t('idea.sa3Max', { max: sa3Max }) }}</span>
          </label>
        </div>
        <!-- YuE2 model precision -->
        <div v-else class="flex flex-wrap items-end gap-4">
          <label v-if="yue2Precisions.length > 1" class="flex flex-col gap-1 text-xs">
            <span class="font-medium text-text">{{ t('idea.precision') }}</span>
            <select v-model="precision" class="rounded-lg border border-border bg-panel-2 px-2.5 py-2 text-sm text-text focus:border-accent1 focus:outline-none">
              <option v-for="p in yue2Precisions" :key="p" :value="p">{{ p }}</option>
            </select>
          </label>
          <p v-if="!instrumental" class="max-w-md text-xs leading-relaxed text-text-dim">{{ t('idea.yue2Length') }}</p>
        </div>

        <div class="flex flex-wrap items-center gap-3 border-t border-border pt-4">
          <span class="text-xs text-text-dim">{{ t('idea.variants') }}</span>
          <ChipGroup v-model="variants" small :options="variantOptions" />
          <button type="button" class="ms-auto rounded-lg border border-border px-3 py-2 text-xs text-text-dim hover:border-text-dim hover:text-text" @click="openInForm">{{ t('idea.openInForm', { engine: MODEL_LABELS[engineInfo.model] }) }}</button>
          <button type="button" class="accent-gradient inline-flex items-center gap-1.5 rounded-lg px-4 py-2 text-sm font-semibold text-white disabled:opacity-50" :disabled="starting || orchestrator.switching || !draft.caption.trim()" @click="generate">
            {{ starting ? (engineRunning(draft.engine) ? t('idea.sending') : t('idea.startingEngine', { engine: MODEL_LABELS[engineInfo.model] })) : t('idea.generate') }}
          </button>
        </div>
        <p v-if="!engineRunning(draft.engine) && orchestrator.activeModel && orchestrator.activeModel !== engineInfo.model" class="-mt-2 text-end text-[11px] text-text-dim">{{ t('aiPart.engineSwitchNote', { running: MODEL_LABELS[orchestrator.activeModel] }) }}</p>
      </div>
    </section>

    <IdeaGuide :open="guideOpen" @close="guideOpen = false" />
  </div>
</template>
