//! The sample formats a stream carries, how PipeWire names them and lays out their channels, and
//! the gain stage the real-time callback applies.

use pipewire::spa::param::audio::AudioFormat;
use pipewire::spa::sys as spas;

/// The most channels a stream takes, which is also the length of its per-channel gains.
pub const MAX_CHANNELS: usize = 8;
pub const MIN_RATE: u32 = 8000;
pub const MAX_RATE: u32 = 192_000;

const WAVE_FORMAT_PCM: u32 = 1;
const WAVE_FORMAT_IEEE_FLOAT: u32 = 3;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Sample {
    U8,
    S16,
    /// Packed in three bytes, as WAVE files store it.
    S24,
    S32,
    F32,
    F64,
}

impl Sample {
    pub fn from_wave(tag: u32, bits: u32) -> Option<Sample> {
        match (tag, bits) {
            (WAVE_FORMAT_PCM, 8) => Some(Sample::U8),
            (WAVE_FORMAT_PCM, 16) => Some(Sample::S16),
            (WAVE_FORMAT_PCM, 24) => Some(Sample::S24),
            (WAVE_FORMAT_PCM, 32) => Some(Sample::S32),
            (WAVE_FORMAT_IEEE_FLOAT, 32) => Some(Sample::F32),
            (WAVE_FORMAT_IEEE_FLOAT, 64) => Some(Sample::F64),
            _ => None,
        }
    }

    pub fn bytes(self) -> usize {
        match self {
            Sample::U8 => 1,
            Sample::S16 => 2,
            Sample::S24 => 3,
            Sample::S32 | Sample::F32 => 4,
            Sample::F64 => 8,
        }
    }

    /// The SPA format of the same layout. S24 must be S24_LE, the packed one: declaring S24_32_LE
    /// for packed data makes PipeWire read every sample from the wrong bytes, which plays as
    /// full-scale noise.
    pub fn spa(self) -> AudioFormat {
        match self {
            Sample::U8 => AudioFormat::U8,
            Sample::S16 => AudioFormat::S16LE,
            Sample::S24 => AudioFormat::S24LE,
            Sample::S32 => AudioFormat::S32LE,
            Sample::F32 => AudioFormat::F32LE,
            Sample::F64 => AudioFormat::F64LE,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Format {
    pub sample: Sample,
    pub channels: u32,
    pub rate: u32,
}

impl Format {
    /// The format of a WAVEFORMATEX's fields, or None when PipeWire is not given it.
    pub fn from_wave(tag: u32, channels: u32, rate: u32, bits: u32) -> Option<Format> {
        let sample = Sample::from_wave(tag, bits)?;
        if channels == 0
            || channels as usize > MAX_CHANNELS
            || !(MIN_RATE..=MAX_RATE).contains(&rate)
        {
            return None;
        }
        Some(Format {
            sample,
            channels,
            rate,
        })
    }

    pub fn stride(&self) -> usize {
        self.sample.bytes() * self.channels as usize
    }

    pub fn positions(&self) -> &'static [u32] {
        LAYOUTS[self.channels as usize - 1]
    }
}

/// Channel positions by channel count. One channel is MONO, which PipeWire plays at full level on
/// every output channel; the others follow the order Windows assumes for a WAVE file without a
/// channel mask, which is the order nvwave's data arrives in.
const LAYOUTS: [&[u32]; MAX_CHANNELS] = [
    &[spas::SPA_AUDIO_CHANNEL_MONO],
    &[spas::SPA_AUDIO_CHANNEL_FL, spas::SPA_AUDIO_CHANNEL_FR],
    &[
        spas::SPA_AUDIO_CHANNEL_FL,
        spas::SPA_AUDIO_CHANNEL_FR,
        spas::SPA_AUDIO_CHANNEL_FC,
    ],
    &[
        spas::SPA_AUDIO_CHANNEL_FL,
        spas::SPA_AUDIO_CHANNEL_FR,
        spas::SPA_AUDIO_CHANNEL_RL,
        spas::SPA_AUDIO_CHANNEL_RR,
    ],
    &[
        spas::SPA_AUDIO_CHANNEL_FL,
        spas::SPA_AUDIO_CHANNEL_FR,
        spas::SPA_AUDIO_CHANNEL_FC,
        spas::SPA_AUDIO_CHANNEL_RL,
        spas::SPA_AUDIO_CHANNEL_RR,
    ],
    &[
        spas::SPA_AUDIO_CHANNEL_FL,
        spas::SPA_AUDIO_CHANNEL_FR,
        spas::SPA_AUDIO_CHANNEL_FC,
        spas::SPA_AUDIO_CHANNEL_LFE,
        spas::SPA_AUDIO_CHANNEL_RL,
        spas::SPA_AUDIO_CHANNEL_RR,
    ],
    &[
        spas::SPA_AUDIO_CHANNEL_FL,
        spas::SPA_AUDIO_CHANNEL_FR,
        spas::SPA_AUDIO_CHANNEL_FC,
        spas::SPA_AUDIO_CHANNEL_LFE,
        spas::SPA_AUDIO_CHANNEL_RL,
        spas::SPA_AUDIO_CHANNEL_RR,
        spas::SPA_AUDIO_CHANNEL_RC,
    ],
    &[
        spas::SPA_AUDIO_CHANNEL_FL,
        spas::SPA_AUDIO_CHANNEL_FR,
        spas::SPA_AUDIO_CHANNEL_FC,
        spas::SPA_AUDIO_CHANNEL_LFE,
        spas::SPA_AUDIO_CHANNEL_RL,
        spas::SPA_AUDIO_CHANNEL_RR,
        spas::SPA_AUDIO_CHANNEL_SL,
        spas::SPA_AUDIO_CHANNEL_SR,
    ],
];

/// Scales every sample of whole frames by its channel's gain and clamps it to the format's range,
/// in place. Runs on the real-time thread, so it allocates nothing and cannot panic.
///
/// Integer samples at unity gain are left as they are, since they cannot be out of range. Float
/// samples are always clamped to -1..1, and a NaN becomes silence: a synthesizer that hands over
/// garbage must not reach the listener at more than full scale.
pub fn apply_gain(sample: Sample, channels: usize, gains: &[f32; MAX_CHANNELS], data: &mut [u8]) {
    let channels = channels.clamp(1, MAX_CHANNELS);
    let gains = &gains[..channels];
    let unity = gains.iter().all(|&g| g == 1.0);
    let frame = sample.bytes() * channels;
    match sample {
        Sample::F32 => {
            for f in data.chunks_exact_mut(frame) {
                for (s, &g) in f.as_chunks_mut::<4>().0.iter_mut().zip(gains) {
                    *s = clamp_unit_f32(f32::from_le_bytes(*s) * g).to_le_bytes();
                }
            }
        }
        Sample::F64 => {
            for f in data.chunks_exact_mut(frame) {
                for (s, &g) in f.as_chunks_mut::<8>().0.iter_mut().zip(gains) {
                    *s = clamp_unit_f64(f64::from_le_bytes(*s) * g as f64).to_le_bytes();
                }
            }
        }
        _ if unity => {}
        Sample::U8 => {
            for f in data.chunks_exact_mut(frame) {
                for (s, &g) in f.iter_mut().zip(gains) {
                    let v = (*s as f32 - 128.0) * g + 128.0;
                    // A float-to-integer cast saturates, which is the clamp.
                    *s = v.round() as u8;
                }
            }
        }
        Sample::S16 => {
            for f in data.chunks_exact_mut(frame) {
                for (s, &g) in f.as_chunks_mut::<2>().0.iter_mut().zip(gains) {
                    *s = ((i16::from_le_bytes(*s) as f32 * g).round() as i16).to_le_bytes();
                }
            }
        }
        Sample::S24 => {
            for f in data.chunks_exact_mut(frame) {
                for (s, &g) in f.as_chunks_mut::<3>().0.iter_mut().zip(gains) {
                    // The top byte filled and shifted back down sign-extends the 24-bit value.
                    let x = i32::from_le_bytes([0, s[0], s[1], s[2]]) >> 8;
                    // A 24-bit integer is exact in an f32.
                    let v = (x as f32 * g).round().clamp(-8_388_608.0, 8_388_607.0) as i32;
                    s.copy_from_slice(&v.to_le_bytes()[..3]);
                }
            }
        }
        Sample::S32 => {
            for f in data.chunks_exact_mut(frame) {
                for (s, &g) in f.as_chunks_mut::<4>().0.iter_mut().zip(gains) {
                    *s = ((i32::from_le_bytes(*s) as f64 * g as f64).round() as i32).to_le_bytes();
                }
            }
        }
    }
}

fn clamp_unit_f32(v: f32) -> f32 {
    if v.is_nan() { 0.0 } else { v.clamp(-1.0, 1.0) }
}

fn clamp_unit_f64(v: f64) -> f64 {
    if v.is_nan() { 0.0 } else { v.clamp(-1.0, 1.0) }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn gains(values: &[f32]) -> [f32; MAX_CHANNELS] {
        let mut g = [1.0; MAX_CHANNELS];
        g[..values.len()].copy_from_slice(values);
        g
    }

    #[test]
    fn only_the_formats_nvwave_can_describe_are_taken() {
        assert_eq!(Sample::from_wave(1, 8), Some(Sample::U8));
        assert_eq!(Sample::from_wave(1, 24), Some(Sample::S24));
        assert_eq!(Sample::from_wave(3, 64), Some(Sample::F64));
        assert_eq!(Sample::from_wave(3, 16), None);
        assert_eq!(Sample::from_wave(2, 16), None);
        assert!(Format::from_wave(1, 2, 44100, 16).is_some());
        assert!(Format::from_wave(1, 0, 44100, 16).is_none());
        assert!(Format::from_wave(1, 9, 44100, 16).is_none());
        assert!(Format::from_wave(1, 1, 7999, 16).is_none());
        assert!(Format::from_wave(1, 1, 192_001, 16).is_none());
        assert_eq!(Format::from_wave(1, 2, 48000, 24).unwrap().stride(), 6);
    }

    #[test]
    fn packed_24_bit_is_declared_as_packed() {
        assert_eq!(Sample::S24.spa(), AudioFormat::S24LE);
        assert_eq!(Sample::S24.bytes(), 3);
    }

    #[test]
    fn every_layout_has_its_channel_count_and_no_repeats() {
        for (i, layout) in LAYOUTS.iter().enumerate() {
            assert_eq!(layout.len(), i + 1);
            let mut seen = layout.to_vec();
            seen.sort_unstable();
            seen.dedup();
            assert_eq!(seen.len(), layout.len());
        }
        assert_eq!(LAYOUTS[0], &[spas::SPA_AUDIO_CHANNEL_MONO]);
    }

    #[test]
    fn s16_scales_per_channel() {
        let mut data: Vec<u8> = [10000i16, -10000, 10000, -10000]
            .iter()
            .flat_map(|v| v.to_le_bytes())
            .collect();
        apply_gain(Sample::S16, 2, &gains(&[0.5, 0.25]), &mut data);
        let out: Vec<i16> = data
            .as_chunks::<2>()
            .0
            .iter()
            .map(|c| i16::from_le_bytes(*c))
            .collect();
        assert_eq!(out, [5000, -2500, 5000, -2500]);
    }

    #[test]
    fn u8_scales_around_its_midpoint() {
        let mut data = vec![228u8, 28, 128];
        apply_gain(Sample::U8, 1, &gains(&[0.5]), &mut data);
        assert_eq!(data, [178, 78, 128]);
    }

    #[test]
    fn s24_keeps_its_sign_and_packing() {
        let mut data = Vec::new();
        for v in [4_000_000i32, -4_000_000, 8_388_607, -8_388_608] {
            data.extend_from_slice(&v.to_le_bytes()[..3]);
        }
        apply_gain(Sample::S24, 1, &gains(&[0.5]), &mut data);
        let out: Vec<i32> = data
            .as_chunks::<3>()
            .0
            .iter()
            .map(|c| i32::from_le_bytes([0, c[0], c[1], c[2]]) >> 8)
            .collect();
        assert_eq!(out, [2_000_000, -2_000_000, 4_194_304, -4_194_304]);
    }

    #[test]
    fn s32_scales_in_double_precision() {
        let mut data: Vec<u8> = [i32::MAX, i32::MIN]
            .iter()
            .flat_map(|v| v.to_le_bytes())
            .collect();
        apply_gain(Sample::S32, 1, &gains(&[0.5]), &mut data);
        let out: Vec<i32> = data
            .as_chunks::<4>()
            .0
            .iter()
            .map(|c| i32::from_le_bytes(*c))
            .collect();
        assert_eq!(out, [1_073_741_824, -1_073_741_824]);
    }

    #[test]
    fn floats_are_clamped_even_at_unity_and_nan_is_silence() {
        let mut data: Vec<u8> = [2.0f32, -3.0, f32::NAN, 0.5]
            .iter()
            .flat_map(|v| v.to_le_bytes())
            .collect();
        apply_gain(Sample::F32, 1, &gains(&[1.0]), &mut data);
        let out: Vec<f32> = data
            .as_chunks::<4>()
            .0
            .iter()
            .map(|c| f32::from_le_bytes(*c))
            .collect();
        assert_eq!(out, [1.0, -1.0, 0.0, 0.5]);
        let mut data: Vec<u8> = [1.5f64, -0.5]
            .iter()
            .flat_map(|v| v.to_le_bytes())
            .collect();
        apply_gain(Sample::F64, 1, &gains(&[0.5]), &mut data);
        let out: Vec<f64> = data
            .as_chunks::<8>()
            .0
            .iter()
            .map(|c| f64::from_le_bytes(*c))
            .collect();
        assert_eq!(out, [0.75, -0.25]);
    }

    #[test]
    fn integers_at_unity_are_untouched() {
        let original: Vec<u8> = (0..=255).collect();
        let mut data = original.clone();
        apply_gain(Sample::S16, 2, &gains(&[1.0, 1.0]), &mut data);
        assert_eq!(data, original);
    }

    #[test]
    fn a_partial_frame_at_the_end_is_left_alone() {
        let mut data = vec![0x10, 0x27, 0x10, 0x27, 0xff];
        apply_gain(Sample::S16, 2, &gains(&[0.5, 0.5]), &mut data);
        assert_eq!(data[4], 0xff);
    }
}
