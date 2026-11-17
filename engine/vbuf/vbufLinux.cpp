// NVDA's virtual buffer storage (nvdaHelper/vbufBase/storage.cpp, unchanged) behind the VBuf_*
// functions virtualBuffers/__init__.py calls, as a Linux shared library loaded into NVDA's process.
//
// Nothing renders here: a renderer (engine/crates/vbufengine) writes a document as a render stream,
// and VBufLinux_build or VBufLinux_replaceSubtree turns the stream into storage nodes. Every entry
// point takes the buffer's lock, as vbufRemote.cpp takes the backend's.
//
// Text is UTF-16 code units, one in each wchar_t, as on Windows: offsets and line lengths then match
// NVDA's there, and upstream's xml.h writes each half of a surrogate pair as an unich element, which
// NVDA's XML parser joins again. The stream and every string crossing this interface are uint16_t.
//
// Render stream:
//   C<docHandle>,<ID>,<flags>,<attributeCount>;<name><value>...   open a control field
//   T<text><attributeCount>;<name><value>...                       a text field in the open field
//   E                                                               close the open control field
// where a string is <length in units>:<units>, flags bit 1 is isBlock and bit 2 is isHidden.

#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <map>
#include <mutex>
#include <string>
#include <vector>

#include "storage.h"

namespace {

struct LinuxBuffer {
	std::mutex lock;
	VBufStorage_buffer_t storage;
};

class StreamReader {
	public:
	StreamReader(const uint16_t* data, size_t length): data(data), end(data + length) {}

	bool op(uint16_t* out) {
		if(data >= end) return false;
		*out = *data++;
		return true;
	}

	bool integer(long long* out, uint16_t terminator) {
		bool negative = false;
		if(data < end && *data == u'-') {
			negative = true;
			++data;
		}
		long long value = 0;
		const uint16_t* start = data;
		// Eighteen digits cannot overflow a long long.
		while(data < end && *data >= u'0' && *data <= u'9' && data - start < 18) {
			value = value * 10 + (*data - u'0');
			++data;
		}
		if(data == start || data >= end || *data != terminator) return false;
		++data;
		*out = negative ? -value : value;
		return true;
	}

	bool string(std::wstring& out) {
		long long length;
		if(!integer(&length, u':') || length < 0 || end - data < length) return false;
		out.assign(data, data + length);
		data += length;
		return true;
	}

	bool attributes(VBufStorage_fieldNode_t* node) {
		long long count;
		if(!integer(&count, u';')) return false;
		std::wstring name, value;
		for(long long i = 0; i < count; ++i) {
			if(!string(name) || !string(value)) return false;
			if(node) node->addAttribute(name, value);
		}
		return true;
	}

	private:
	const uint16_t* data;
	const uint16_t* end;
};

// Builds the stream into storage, which must be empty. Returns false on a malformed stream, leaving
// whatever was built before the fault.
bool buildFromStream(VBufStorage_buffer_t& storage, const uint16_t* data, size_t length) {
	StreamReader reader(data, length);
	std::vector<VBufStorage_controlFieldNode_t*> parents;
	std::vector<VBufStorage_fieldNode_t*> previous;
	parents.push_back(nullptr);
	previous.push_back(nullptr);
	uint16_t op;
	std::wstring text;
	while(reader.op(&op)) {
		switch(op) {
			case u'C': {
				long long docHandle, ID, flags;
				if(!reader.integer(&docHandle, u',') || !reader.integer(&ID, u',') || !reader.integer(&flags, u',')) return false;
				bool insideRefused = parents.size() > 1 && !parents.back();
				VBufStorage_controlFieldNode_t* node = nullptr;
				if(!insideRefused) node = storage.addControlFieldNode(parents.back(), previous.back(), (int)docHandle, (int)ID, (flags & 1) != 0);
				// A repeated identifier is refused; its subtree is still read, into nothing.
				if(node) node->isHidden = (flags & 2) != 0;
				if(!reader.attributes(node)) return false;
				if(node) previous.back() = node;
				parents.push_back(node);
				previous.push_back(nullptr);
				break;
			}
			case u'T': {
				if(!reader.string(text)) return false;
				VBufStorage_textFieldNode_t* node = nullptr;
				if(parents.back() && !text.empty()) node = storage.addTextFieldNode(parents.back(), previous.back(), text);
				if(!reader.attributes(node)) return false;
				if(node) previous.back() = node;
				break;
			}
			case u'E': {
				if(parents.size() <= 1) return false;
				parents.pop_back();
				previous.pop_back();
				break;
			}
			default:
				return false;
		}
	}
	return parents.size() == 1;
}

std::wstring fromUnits(const uint16_t* units, size_t length) {
	return units ? std::wstring(units, units + length) : std::wstring();
}

}  // namespace

extern "C" {

typedef uint64_t VBufLinux_nodeHandle_t;

void* VBufLinux_create() {
	return new LinuxBuffer();
}

void VBufLinux_destroy(void* buffer) {
	auto b = static_cast<LinuxBuffer*>(buffer);
	{
		std::lock_guard<std::mutex> guard(b->lock);
		b->storage.clearBuffer();
	}
	delete b;
}

// Builds the stream into the buffer, which must be empty. Returns 1 when built.
int VBufLinux_build(void* buffer, const uint16_t* stream, size_t length) {
	auto b = static_cast<LinuxBuffer*>(buffer);
	std::lock_guard<std::mutex> guard(b->lock);
	return buildFromStream(b->storage, stream, length) ? 1 : 0;
}

// Replaces the control field docHandle, ID with the stream, which renders that field anew. Returns
// 0 when the field is not in the buffer or the stream is malformed, and leaves the buffer as it was.
int VBufLinux_replaceSubtree(void* buffer, int docHandle, int ID, const uint16_t* stream, size_t length) {
	auto b = static_cast<LinuxBuffer*>(buffer);
	auto temp = new VBufStorage_buffer_t();
	if(!buildFromStream(*temp, stream, length)) {
		temp->clearBuffer();
		delete temp;
		return 0;
	}
	std::lock_guard<std::mutex> guard(b->lock);
	auto node = b->storage.getControlFieldNodeWithIdentifier(docHandle, ID);
	if(!node) {
		temp->clearBuffer();
		delete temp;
		return 0;
	}
	// An identifier the new subtree shares with the rest of the buffer would be indexed twice.
	std::map<VBufStorage_fieldNode_t*, VBufStorage_buffer_t*> m{{node, temp}};
	return b->storage.replaceSubtrees(m) ? 1 : 0;
}

int VBuf_getFieldNodeOffsets(void* buffer, VBufLinux_nodeHandle_t node, int* startOffset, int* endOffset) {
	auto b = static_cast<LinuxBuffer*>(buffer);
	std::lock_guard<std::mutex> guard(b->lock);
	return b->storage.getFieldNodeOffsets((VBufStorage_fieldNode_t*)node, startOffset, endOffset);
}

int VBuf_isFieldNodeAtOffset(void* buffer, VBufLinux_nodeHandle_t node, int offset) {
	auto b = static_cast<LinuxBuffer*>(buffer);
	std::lock_guard<std::mutex> guard(b->lock);
	return b->storage.isFieldNodeAtOffset((VBufStorage_fieldNode_t*)node, offset);
}

int VBuf_locateTextFieldNodeAtOffset(void* buffer, int offset, int* startOffset, int* endOffset, VBufLinux_nodeHandle_t* foundNode) {
	auto b = static_cast<LinuxBuffer*>(buffer);
	std::lock_guard<std::mutex> guard(b->lock);
	*foundNode = (VBufLinux_nodeHandle_t)b->storage.locateTextFieldNodeAtOffset(offset, startOffset, endOffset);
	return *foundNode != 0;
}

int VBuf_locateControlFieldNodeAtOffset(void* buffer, int offset, int* startOffset, int* endOffset, int* docHandle, int* ID, VBufLinux_nodeHandle_t* foundNode) {
	auto b = static_cast<LinuxBuffer*>(buffer);
	std::lock_guard<std::mutex> guard(b->lock);
	*foundNode = (VBufLinux_nodeHandle_t)b->storage.locateControlFieldNodeAtOffset(offset, startOffset, endOffset, docHandle, ID);
	return *foundNode != 0;
}

int VBuf_getControlFieldNodeWithIdentifier(void* buffer, int docHandle, int ID, VBufLinux_nodeHandle_t* foundNode) {
	auto b = static_cast<LinuxBuffer*>(buffer);
	std::lock_guard<std::mutex> guard(b->lock);
	*foundNode = (VBufLinux_nodeHandle_t)b->storage.getControlFieldNodeWithIdentifier(docHandle, ID);
	return *foundNode != 0;
}

int VBuf_getIdentifierFromControlFieldNode(void* buffer, VBufLinux_nodeHandle_t node, int* docHandle, int* ID) {
	auto b = static_cast<LinuxBuffer*>(buffer);
	std::lock_guard<std::mutex> guard(b->lock);
	return b->storage.getIdentifierFromControlFieldNode((VBufStorage_controlFieldNode_t*)node, docHandle, ID);
}

int VBuf_findNodeByAttributes(void* buffer, int offset, int direction, const uint16_t* attribs, size_t attribsLength, const uint16_t* regexp, size_t regexpLength, int* startOffset, int* endOffset, VBufLinux_nodeHandle_t* foundNode) {
	auto b = static_cast<LinuxBuffer*>(buffer);
	std::wstring attribsText = fromUnits(attribs, attribsLength);
	std::wstring regexpText = fromUnits(regexp, regexpLength);
	std::lock_guard<std::mutex> guard(b->lock);
	*foundNode = (VBufLinux_nodeHandle_t)b->storage.findNodeByAttributes(offset, (VBufStorage_findDirection_t)direction, attribsText, regexpText, startOffset, endOffset);
	return *foundNode != 0;
}

int VBuf_getSelectionOffsets(void* buffer, int* startOffset, int* endOffset) {
	auto b = static_cast<LinuxBuffer*>(buffer);
	std::lock_guard<std::mutex> guard(b->lock);
	return b->storage.getSelectionOffsets(startOffset, endOffset);
}

int VBuf_setSelectionOffsets(void* buffer, int startOffset, int endOffset) {
	auto b = static_cast<LinuxBuffer*>(buffer);
	std::lock_guard<std::mutex> guard(b->lock);
	return b->storage.setSelectionOffsets(startOffset, endOffset);
}

int VBuf_getTextLength(void* buffer) {
	auto b = static_cast<LinuxBuffer*>(buffer);
	std::lock_guard<std::mutex> guard(b->lock);
	return b->storage.getTextLength();
}

// The text, with markup when asked, as UTF-16 units in memory the caller frees with VBufLinux_free,
// and its length in units; null when empty.
uint16_t* VBuf_getTextInRange(void* buffer, int startOffset, int endOffset, int useMarkup, size_t* length) {
	auto b = static_cast<LinuxBuffer*>(buffer);
	std::wstring text;
	*length = 0;
	{
		std::lock_guard<std::mutex> guard(b->lock);
		b->storage.getTextInRange(startOffset, endOffset, text, useMarkup != 0);
	}
	if(text.empty()) return nullptr;
	auto out = static_cast<uint16_t*>(malloc(text.size() * sizeof(uint16_t)));
	if(!out) return nullptr;
	for(size_t i = 0; i < text.size(); ++i) out[i] = (uint16_t)text[i];
	*length = text.size();
	return out;
}

void VBufLinux_free(void* p) {
	free(p);
}

int VBuf_getLineOffsets(void* buffer, int offset, int maxLineLength, int useScreenLayout, int* startOffset, int* endOffset) {
	auto b = static_cast<LinuxBuffer*>(buffer);
	std::lock_guard<std::mutex> guard(b->lock);
	return b->storage.getLineOffsets(offset, maxLineLength, useScreenLayout != 0, startOffset, endOffset);
}

}  // extern "C"
