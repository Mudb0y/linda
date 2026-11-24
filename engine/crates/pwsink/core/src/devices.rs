//! The output devices libpwsink lists: the sinks in PipeWire's registry, kept by the loop thread as
//! the registry announces and removes them.
//!
//! A device is named as pwsink_open takes it, by its node.name, which target.object matches, and
//! shown by its node.description. A sink without a description is shown by its name, as
//! PipeWire's PulseAudio server shows it to pavucontrol and other mixers. A sink without a name, or
//! with a name that is not UTF-8, is left out, since nothing could choose it again by that name.

use std::ffi::CString;

/// The media class of the nodes listed: sinks, which play audio to a device or to whatever a
/// virtual sink feeds.
const SINK_CLASS: &str = "Audio/Sink";

/// One sink, as the C interface hands it to Python.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Device {
    /// The node.name, which pwsink_open takes as its target.
    pub name: CString,
    /// What to show for it.
    pub description: CString,
}

impl Device {
    /// The device a registry global's properties describe, if it is a sink that can be named.
    /// get looks a property up, and gives None for one that is absent or not UTF-8.
    pub fn from_properties<'a>(get: impl Fn(&str) -> Option<&'a str>) -> Option<Device> {
        if get("media.class")? != SINK_CLASS {
            return None;
        }
        let name = get("node.name").filter(|name| !name.is_empty())?;
        let description = get("node.description")
            .filter(|description| !description.is_empty())
            .unwrap_or(name);
        Some(Device {
            name: CString::new(name).ok()?,
            description: CString::new(description).ok()?,
        })
    }
}

/// The sinks present, by registry id, in the order the registry announced them.
#[derive(Default)]
pub struct Devices {
    present: Vec<(u32, Device)>,
}

impl Devices {
    /// A global the registry announced. An id is reused only after its global was removed, but a
    /// device already kept under it is replaced all the same, so that a missed removal leaves no
    /// stale entry.
    pub fn add(&mut self, id: u32, device: Device) {
        self.remove(id);
        self.present.push((id, device));
    }

    /// A global the registry removed, which is not necessarily a sink.
    pub fn remove(&mut self, id: u32) {
        self.present.retain(|(kept, _)| *kept != id);
    }

    /// Forgets everything, as when the connection is lost.
    pub fn clear(&mut self) {
        self.present.clear();
    }

    pub fn list(&self) -> Vec<Device> {
        self.present.iter().map(|(_, d)| d.clone()).collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn props<'a>(pairs: &'a [(&'a str, &'a str)]) -> impl Fn(&str) -> Option<&'a str> {
        |key| pairs.iter().find(|(k, _)| *k == key).map(|(_, v)| *v)
    }

    fn device(name: &str, description: &str) -> Device {
        Device {
            name: CString::new(name).unwrap(),
            description: CString::new(description).unwrap(),
        }
    }

    #[test]
    fn a_sink_is_named_by_its_node_name_and_shown_by_its_description() {
        let found = Device::from_properties(props(&[
            ("media.class", "Audio/Sink"),
            ("node.name", "alsa_output.usb-headset.analog-stereo"),
            ("node.description", "USB Headset Analog Stereo"),
        ]));
        assert_eq!(
            found,
            Some(device(
                "alsa_output.usb-headset.analog-stereo",
                "USB Headset Analog Stereo"
            ))
        );
    }

    #[test]
    fn a_sink_without_a_description_is_shown_by_its_name() {
        for description in [None, Some("")] {
            let mut pairs = vec![("media.class", "Audio/Sink"), ("node.name", "plain-sink")];
            if let Some(d) = description {
                pairs.push(("node.description", d));
            }
            assert_eq!(
                Device::from_properties(props(&pairs)),
                Some(device("plain-sink", "plain-sink"))
            );
        }
    }

    #[test]
    fn only_sinks_with_a_name_are_listed() {
        for pairs in [
            &[
                ("media.class", "Stream/Output/Audio"),
                ("node.name", "linda"),
            ][..],
            &[("media.class", "Audio/Source"), ("node.name", "mic")][..],
            &[("node.name", "no-class")][..],
            &[("media.class", "Audio/Sink")][..],
            &[("media.class", "Audio/Sink"), ("node.name", "")][..],
        ] {
            assert_eq!(Device::from_properties(props(pairs)), None, "{pairs:?}");
        }
    }

    #[test]
    fn devices_come_and_go_by_registry_id() {
        let mut devices = Devices::default();
        devices.add(40, device("a", "A"));
        devices.add(41, device("b", "B"));
        devices.add(42, device("c", "C"));
        devices.remove(41);
        devices.remove(7);
        assert_eq!(devices.list(), [device("a", "A"), device("c", "C")]);
        devices.add(40, device("d", "D"));
        assert_eq!(devices.list(), [device("c", "C"), device("d", "D")]);
        devices.clear();
        assert_eq!(devices.list(), []);
    }
}
