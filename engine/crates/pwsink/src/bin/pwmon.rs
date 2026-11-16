//! Records what a sink plays, from its monitor, for tests run against a private PipeWire daemon.
//!
//! usage: pwmon SINK [--raw FILE] [--passive]
//!
//! Prints JSON lines on standard output: the stream's state changes; "start" with the graph time
//! of the first frame recorded; "sound" when any channel rises above -60 dBFS after silence and
//! "silence" when a whole buffer falls below it after sound, each with the graph time of the frame
//! where it changed; and "quantum" whenever the frames per cycle change. Graph times are
//! nanoseconds on CLOCK_MONOTONIC. With --raw, FILE receives the recording as interleaved stereo
//! 32-bit floats at 48 kHz, with silence filled in wherever the graph did not run, so that frame n
//! of the file is at the start time plus n / 48000 seconds. Without --passive the monitor keeps
//! the graph running, which keeps that timeline continuous. It exits when its stream fails, as it
//! does when the daemon goes away.

use std::cell::RefCell;
use std::fs::File;
use std::io::Write;

use pipewire as pw;
use pw::{properties::properties, spa};
use spa::pod::Pod;

const RATE: u32 = 48000;
const THRESHOLD: f32 = 0.001;
/// The longest gap in the graph filled with silence, so that a stalled graph cannot fill the disk.
const MAX_GAP_FRAMES: u64 = 60 * RATE as u64;

struct Recorder {
    sounding: bool,
    last_sound_ns: i64,
    next_ns: Option<i64>,
    quantum: u64,
    raw: Option<File>,
}

impl Recorder {
    fn frame_ns(frames: u64) -> i64 {
        (frames as i128 * 1_000_000_000 / RATE as i128) as i64
    }

    fn take(&mut self, base: i64, samples: &[f32]) {
        let frames = samples.len() / 2;
        if frames as u64 != self.quantum {
            self.quantum = frames as u64;
            println!(r#"{{"event":"quantum","frames":{frames},"graph_ns":{base}}}"#);
        }
        match self.next_ns {
            None => println!(r#"{{"event":"start","graph_ns":{base}}}"#),
            Some(expected) if base > expected + Self::frame_ns(1) => {
                let gap = ((base - expected) as i128 * RATE as i128 / 1_000_000_000) as u64;
                self.write_silence(gap.min(MAX_GAP_FRAMES));
            }
            _ => {}
        }
        self.next_ns = Some(base + Self::frame_ns(frames as u64));
        for (i, frame) in samples.as_chunks::<2>().0.iter().enumerate() {
            let t = base + Self::frame_ns(i as u64);
            let sound = frame[0].abs() > THRESHOLD || frame[1].abs() > THRESHOLD;
            if sound && !self.sounding {
                self.sounding = true;
                println!(r#"{{"event":"sound","graph_ns":{t}}}"#);
            }
            if sound {
                self.last_sound_ns = t;
            }
        }
        if self.sounding && frames > 0 && samples.iter().all(|x| x.abs() <= THRESHOLD) {
            self.sounding = false;
            let t = self.last_sound_ns + Self::frame_ns(1);
            println!(r#"{{"event":"silence","graph_ns":{t}}}"#);
        }
        if let Some(raw) = self.raw.as_mut() {
            let bytes: Vec<u8> = samples.iter().flat_map(|x| x.to_le_bytes()).collect();
            raw.write_all(&bytes).expect("writing the recording");
        }
    }

    fn write_silence(&mut self, frames: u64) {
        if let Some(raw) = self.raw.as_mut() {
            raw.write_all(&vec![0u8; frames as usize * 8])
                .expect("writing the recording");
        }
    }
}

fn main() -> Result<(), pw::Error> {
    let mut args = std::env::args().skip(1);
    let target = args
        .next()
        .expect("usage: pwmon SINK [--raw FILE] [--passive]");
    let mut raw = None;
    let mut passive = false;
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--raw" => {
                let path = args.next().expect("--raw needs a file");
                raw = Some(File::create(path).expect("creating the recording"));
            }
            "--passive" => passive = true,
            other => panic!("unknown argument {other}"),
        }
    }
    pw::init();
    let mainloop = pw::main_loop::MainLoopRc::new(None)?;
    let context = pw::context::ContextRc::new(&mainloop, None)?;
    let core = context.connect_rc(None)?;
    let quit = mainloop.clone();
    let _core_listener = core
        .add_listener_local()
        .error(move |id, _seq, res, _message| {
            if id == pw::core::PW_ID_CORE && res == -libc::EPIPE {
                quit.quit();
            }
        })
        .register();
    let stream = pw::stream::StreamBox::new(
        &core,
        "pwmon",
        properties! {
            *pw::keys::MEDIA_TYPE => "Audio",
            *pw::keys::MEDIA_CATEGORY => "Capture",
            *pw::keys::MEDIA_ROLE => "Production",
            *pw::keys::NODE_NAME => "pwmon",
            *pw::keys::STREAM_CAPTURE_SINK => "true",
            "target.object" => target.as_str(),
            "node.passive" => if passive { "true" } else { "false" },
            "state.restore-props" => "false",
            "state.restore-target" => "false",
        },
    )?;
    let recorder = RefCell::new(Recorder {
        sounding: false,
        last_sound_ns: 0,
        next_ns: None,
        quantum: 0,
        raw,
    });
    let quit = mainloop.clone();
    let _listener = stream
        .add_local_listener_with_user_data(())
        .state_changed(move |_, _, old, new| {
            println!(
                r#"{{"event":"state","old":"{old:?}","new":"{new:?}","mono_ns":{}}}"#,
                pwsink::mono_ns()
            );
            if matches!(new, pw::stream::StreamState::Error(_)) {
                quit.quit();
            }
        })
        .process(move |stream, _| {
            let now = stream.time().map(|t| t.now()).unwrap_or(0);
            // SAFETY: the buffer is dequeued and queued again within this callback.
            let b = unsafe { stream.dequeue_raw_buffer() };
            if b.is_null() {
                return;
            }
            unsafe {
                let d = &*(*(*b).buffer).datas;
                // The graph time of the cycle that produced this data, or this cycle's.
                let base = if (*b).time > 0 { (*b).time as i64 } else { now };
                if !d.data.is_null() {
                    let size = (*d.chunk).size as usize;
                    let samples = std::slice::from_raw_parts(d.data as *const f32, size / 4);
                    recorder.borrow_mut().take(base, samples);
                }
                stream.queue_raw_buffer(b);
            }
        })
        .register()?;

    let mut info = spa::param::audio::AudioInfoRaw::new();
    info.set_format(spa::param::audio::AudioFormat::F32LE);
    info.set_rate(RATE);
    info.set_channels(2);
    let mut position = [0; spa::param::audio::MAX_CHANNELS];
    position[0] = spa::sys::SPA_AUDIO_CHANNEL_FL;
    position[1] = spa::sys::SPA_AUDIO_CHANNEL_FR;
    info.set_position(position);
    let values: Vec<u8> = spa::pod::serialize::PodSerializer::serialize(
        std::io::Cursor::new(Vec::new()),
        &spa::pod::Value::Object(spa::pod::Object {
            type_: spa::sys::SPA_TYPE_OBJECT_Format,
            id: spa::sys::SPA_PARAM_EnumFormat,
            properties: info.into(),
        }),
    )
    .expect("a format pod")
    .0
    .into_inner();
    let mut params = [Pod::from_bytes(&values).expect("a format pod")];
    stream.connect(
        spa::utils::Direction::Input,
        None,
        pw::stream::StreamFlags::AUTOCONNECT
            | pw::stream::StreamFlags::MAP_BUFFERS
            | pw::stream::StreamFlags::RT_PROCESS,
        &mut params,
    )?;
    mainloop.run();
    Ok(())
}
