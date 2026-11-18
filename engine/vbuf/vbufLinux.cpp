// NVDA's virtual buffer storage (nvdaHelper/vbufBase/storage.cpp, unchanged) behind the VBuf_*
// functions virtualBuffers/__init__.py calls, as a Linux shared library loaded into NVDA's process.
//
// Nothing renders here: a renderer (engine/crates/vbufengine) writes a document as a render stream,
// VBufLinux_build turns the stream into storage nodes, and VBufLinux_update splices the re-rendered
// subtrees of an update batch in, as backend.cpp's update does on Windows. Every entry point takes the
// buffer's lock, as vbufRemote.cpp takes the backend's.
//
// On Windows a bad handle or a bad offset from NVDA faulted the browser's process, behind RPC; here it
// would fault NVDA's own. So every buffer handle is looked up among the live buffers, every node handle
// among its buffer's nodes, and no exception leaves an entry point. A buffer is destroyed only once no
// call holds it: calls hold the registry shared, destroy holds it exclusively.
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
// An update batch is a sequence of
//   S<docHandle>,<ID>;   followed by one C to E, which replaces the existing field with that identifier
// and, inside those subtrees only,
//   R<docHandle>,<ID>;   the existing field with that identifier and its subtree, reused in place.

#include <algorithm>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <map>
#include <mutex>
#include <shared_mutex>
#include <string>
#include <unordered_set>
#include <utility>
#include <vector>

#include "storage.h"

namespace {

bool isStrictDescendant(VBufStorage_fieldNode_t* node, VBufStorage_fieldNode_t* ancestor) {
	for(auto parent = node->getParent(); parent; parent = parent->getParent()) {
		if(parent == ancestor) return true;
	}
	return false;
}

class LinuxBuffer final: public VBufStorage_buffer_t {
	public:
	std::mutex lock;
	// The fields an update batch replaces, as backend.cpp's list of the subtrees being re-rendered.
	VBufStorage_controlFieldNodeList_t workingInvalidSubtreesList;

	VBufStorage_controlFieldNode_t* root() { return dynamic_cast<VBufStorage_controlFieldNode_t*>(rootNode); }

	// Takes other's fields in place of this buffer's, and gives it this buffer's, when this buffer's root
	// has the identifier docHandle, ID: as replaceSubtrees replaces the root, with the caret kept the
	// same way, but without touching a node, so that NVDA's main thread is not held while a whole
	// document is built or freed.
	bool adopt(LinuxBuffer& other, int docHandle, int ID) {
		auto old = root();
		int oldDocHandle, oldID;
		if(!old || !other.hasContent() || !old->getIdentifier(&oldDocHandle, &oldID) || oldDocHandle != docHandle || oldID != ID) return false;
		// replaceSubtrees: the caret's offset into each of its fields, from the root down.
		VBufStorage_relativeSelection_t identifierList;
		if(getTextLength() > 0) {
			int start, end, fieldDocHandle, fieldID;
			auto field = locateControlFieldNodeAtOffset(selectionStart, &start, &end, &fieldDocHandle, &fieldID);
			int relative = selectionStart - start;
			for(; field; field = field->getParent()) {
				identifierList.emplace_front(field->identifier, relative);
				for(auto previous = field->getPrevious(); previous; previous = previous->getPrevious()) relative += previous->getLength();
			}
		}
		std::swap(rootNode, other.rootNode);
		std::swap(nodes, other.nodes);
		std::swap(controlFieldNodesByIdentifier, other.controlFieldNodesByIdentifier);
		std::swap(referenceNodes, other.referenceNodes);
		VBufStorage_controlFieldNode_t* deepest = nullptr;
		int relative = 0;
		for(auto& [identifier, offset]: identifierList) {
			auto field = getControlFieldNodeWithIdentifier(identifier.docHandle, identifier.ID);
			if(!field || field->getParent() != deepest) break;
			deepest = field;
			relative = offset;
		}
		int start, end;
		if(deepest && getFieldNodeOffsets(deepest, &start, &end)) selectionStart = start + std::min(relative, std::max(deepest->getLength() - 1, 0));
		return true;
	}

	// backend.cpp VBufBackend_t::reuseExistingNodeInRender, unchanged.
	VBufStorage_controlFieldNode_t* reuseExistingNodeInRender(VBufStorage_controlFieldNode_t* parent, VBufStorage_fieldNode_t* previous, int docHandle, int ID) {
		if(!parent) return nullptr;
		if(parent->alwaysRerenderDescendants || parent->alwaysRerenderChildren) return nullptr;
		auto existingNode = this->getControlFieldNodeWithIdentifier(docHandle, ID);
		if(!existingNode) return nullptr;
		auto existingParent = existingNode->getParent();
		if(!existingParent) return nullptr;
		if(existingParent->alwaysRerenderDescendants) existingNode->alwaysRerenderDescendants = true;
		if(existingNode->alwaysRerenderDescendants) return nullptr;
		if(!existingNode->allowReuseInAncestorUpdate) return nullptr;
		if(existingNode->denyReuseIfPreviousSiblingsChanged) {
			VBufStorage_controlFieldNode_t* previousControlFieldNode = nullptr;
			for(auto tempNode = previous; tempNode != nullptr; tempNode = tempNode->getPrevious()) {
				previousControlFieldNode = dynamic_cast<VBufStorage_controlFieldNode_t*>(tempNode);
				if(previousControlFieldNode) break;
			}
			auto previousReferenceNode = dynamic_cast<VBufStorage_referenceNode_t*>(previousControlFieldNode);
			if(previousControlFieldNode && !previousReferenceNode) return nullptr;
			if(previousReferenceNode) previousControlFieldNode = previousReferenceNode->referenceNode;
			VBufStorage_controlFieldNode_t* previousExistingControlFieldNode = nullptr;
			for(auto tempNode = existingNode->getPrevious(); tempNode != nullptr; tempNode = tempNode->getPrevious()) {
				previousExistingControlFieldNode = dynamic_cast<VBufStorage_controlFieldNode_t*>(tempNode);
				if(previousExistingControlFieldNode) break;
			}
			if(previousControlFieldNode != previousExistingControlFieldNode) return nullptr;
		}
		for(auto i = workingInvalidSubtreesList.begin(); i != workingInvalidSubtreesList.end(); ++i) {
			if(*i == existingNode) {
				workingInvalidSubtreesList.erase(i);
				return nullptr;
			}
		}
		return existingNode;
	}
};

std::shared_mutex registryLock;
std::unordered_set<void*> liveBuffers;

// f(buffer) under the buffer's lock when handle is a live buffer, else fallback. An exception, from
// std::regex or an allocation, must not unwind into the ctypes frame that called us.
template<typename R, typename F>
R withBuffer(void* handle, R fallback, F f) {
	try {
		std::shared_lock registry(registryLock);
		if(!handle || !liveBuffers.count(handle)) return fallback;
		auto b = static_cast<LinuxBuffer*>(handle);
		std::lock_guard<std::mutex> guard(b->lock);
		return f(*b);
	} catch(...) {
		return fallback;
	}
}

class StreamReader {
	public:
	StreamReader(const uint16_t* data, size_t length): data(data), end(data ? data + length : data) {}

	bool atEnd() const { return data >= end; }

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
		if(!integer(&count, u';') || count < 0) return false;
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

// What R ops in the subtree being read may reuse. target is the field its S replaces, null when that
// field is not in the buffer, in which case the whole subtree is read into nothing.
struct Reuse {
	LinuxBuffer* buffer;
	VBufStorage_controlFieldNode_t* target;
	std::vector<VBufStorage_controlFieldNode_t*>* reused;
};

// The reference node standing for the existing field docHandle, ID, or null when it may not be
// reused here. replaceSubtrees moves each referenced field into place; it trusts the renderer, so a
// field outside the replaced one, or one inside another reused field, would leave a cycle or a freed
// node in the tree, and is refused here first.
VBufStorage_fieldNode_t* reuseNode(Reuse& reuse, VBufStorage_buffer_t& temp, VBufStorage_controlFieldNode_t* parent, VBufStorage_fieldNode_t* previous, int docHandle, int ID) {
	auto existing = reuse.buffer->getControlFieldNodeWithIdentifier(docHandle, ID);
	if(!existing || !isStrictDescendant(existing, reuse.target)) return nullptr;
	for(auto other: *reuse.reused) {
		if(other == existing || isStrictDescendant(existing, other) || isStrictDescendant(other, existing)) return nullptr;
	}
	if(reuse.buffer->reuseExistingNodeInRender(parent, previous, docHandle, ID) != existing) return nullptr;
	auto reference = temp.addReferenceNodeToBuffer(parent, previous, existing);
	if(!reference) return nullptr;
	reuse.reused->push_back(existing);
	return reference;
}

// Fields nested deeper than this are a malformed stream. storage.cpp recurses once a level to find,
// read and delete text, on whichever of NVDA's threads asks, so a buffer nested without bound would
// overflow its stack and take NVDA's process with it. The engine writes nothing deeper than about a
// thousand (render.rs MAX_DEPTH).
constexpr size_t MAX_NESTING = 4096;

size_t depthOf(VBufStorage_fieldNode_t* node) {
	size_t depth = 0;
	for(auto parent = node ? node->getParent() : nullptr; parent; parent = parent->getParent()) ++depth;
	return depth;
}

// Reads C, T and E ops into storage. With reuse, reads exactly one top-level field, which may hold R
// ops, and stops after it; without, reads to the end of the stream, where R and S are malformed, or
// with oneField only one top-level field and stops after it.
bool readFields(StreamReader& reader, VBufStorage_buffer_t& storage, Reuse* reuse, bool oneField = false) {
	oneField = oneField || reuse;
	std::vector<VBufStorage_controlFieldNode_t*> parents;
	std::vector<VBufStorage_fieldNode_t*> previous;
	parents.push_back(nullptr);
	previous.push_back(nullptr);
	bool discard = reuse && !reuse->target;
	size_t maxParents = MAX_NESTING + 1 - (reuse ? std::min(depthOf(reuse->target), MAX_NESTING) : 0);
	uint16_t op;
	std::wstring text;
	while(reader.op(&op)) {
		if(oneField && parents.size() == 1 && op != u'C') return false;
		switch(op) {
			case u'C': {
				long long docHandle, ID, flags;
				if(!reader.integer(&docHandle, u',') || !reader.integer(&ID, u',') || !reader.integer(&flags, u',')) return false;
				if(parents.size() >= maxParents) return false;
				bool insideRefused = parents.size() > 1 && !parents.back();
				VBufStorage_controlFieldNode_t* node = nullptr;
				if(!discard && !insideRefused) node = storage.addControlFieldNode(parents.back(), previous.back(), (int)docHandle, (int)ID, (flags & 1) != 0);
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
			case u'R': {
				long long docHandle, ID;
				if(!reuse || parents.size() <= 1) return false;
				if(!reader.integer(&docHandle, u',') || !reader.integer(&ID, u';')) return false;
				if(!parents.back()) break;
				auto node = reuseNode(*reuse, storage, parents.back(), previous.back(), (int)docHandle, (int)ID);
				// Nothing was rendered in its place, so a refused reuse cannot fall back to rendering.
				if(!node) return false;
				previous.back() = node;
				break;
			}
			case u'E': {
				if(parents.size() <= 1) return false;
				parents.pop_back();
				previous.pop_back();
				if(oneField && parents.size() == 1) return true;
				break;
			}
			default:
				return false;
		}
	}
	return !oneField && parents.size() == 1;
}

void clampSelection(VBufStorage_buffer_t& storage) {
	int start, end;
	storage.getSelectionOffsets(&start, &end);
	int length = storage.getTextLength();
	int last = length > 0 ? length - 1 : 0;
	if(start > last) storage.setSelectionOffsets(last, last);
}

// Applies an update batch: see the stream's description above. All replacements go through one
// replaceSubtrees call, which keeps the caret relative to the deepest of its fields that survived.
// Returns 1 when every S was applied; 0 when the batch is malformed, which changes nothing, or when an
// S named a field the buffer no longer has, which skips that S alone.
int applyUpdate(LinuxBuffer& b, const uint16_t* stream, size_t length) {
	std::vector<std::pair<VBufStorage_controlFieldNode_t*, VBufStorage_buffer_t*>> replacements;
	std::vector<VBufStorage_controlFieldNode_t*> reused;
	auto discardAll = [&]() {
		for(auto& r: replacements) delete r.second;
		b.workingInvalidSubtreesList.clear();
		return 0;
	};
	b.workingInvalidSubtreesList.clear();
	StreamReader reader(stream, length);
	bool allFound = true;
	uint16_t op;
	while(reader.op(&op)) {
		long long docHandle, ID;
		if(op != u'S' || !reader.integer(&docHandle, u',') || !reader.integer(&ID, u';')) return discardAll();
		auto target = b.getControlFieldNodeWithIdentifier((int)docHandle, (int)ID);
		if(target) b.workingInvalidSubtreesList.push_back(target);
		auto temp = new VBufStorage_buffer_t();
		Reuse reuse{&b, target, &reused};
		bool read = readFields(reader, *temp, &reuse);
		if(target) replacements.emplace_back(target, temp);
		else delete temp;
		if(!read) return discardAll();
		if(!target) allFound = false;
	}
	if(replacements.empty()) {
		b.workingInvalidSubtreesList.clear();
		return allFound ? 1 : 0;
	}
	// replaceSubtrees deletes each replaced field with its subtree, so a field replaced twice, or inside
	// another replaced field, would be freed while still in its map.
	for(size_t i = 0; i < replacements.size(); ++i) {
		for(size_t j = 0; j < replacements.size(); ++j) {
			if(i == j) continue;
			auto a = replacements[i].first;
			auto c = replacements[j].first;
			if(a == c || isStrictDescendant(a, c)) return discardAll();
		}
	}
	// replaceSubtrees makes room for an identifier the buffer already has by removing the field that
	// holds it and keeping its children, which it cannot do for the root. It then leaves the new field
	// out of the identifier map, which still names the root, and a later update frees the wrong field
	// or never returns. So only a replacement of the root itself may carry the root's identifier.
	if(auto root = b.root()) {
		int rootDocHandle, rootID;
		root->getIdentifier(&rootDocHandle, &rootID);
		for(auto& r: replacements) {
			if(r.first != root && r.second->getControlFieldNodeWithIdentifier(rootDocHandle, rootID)) return discardAll();
		}
	}
	std::map<VBufStorage_fieldNode_t*, VBufStorage_buffer_t*> m;
	for(auto& r: replacements) m[r.first] = r.second;
	// The map owns the temporary buffers from here: replaceSubtrees deletes each.
	replacements.clear();
	bool replaced = b.replaceSubtrees(m);
	b.workingInvalidSubtreesList.clear();
	clampSelection(b);
	return replaced && allFound ? 1 : 0;
}

}  // namespace

extern "C" {

typedef uint64_t VBufLinux_nodeHandle_t;

void* VBufLinux_create() {
	try {
		auto b = new LinuxBuffer();
		std::unique_lock registry(registryLock);
		liveBuffers.insert(b);
		return b;
	} catch(...) {
		return nullptr;
	}
}

void VBufLinux_destroy(void* buffer) {
	LinuxBuffer* b = nullptr;
	{
		std::unique_lock registry(registryLock);
		if(!buffer || !liveBuffers.erase(buffer)) return;
		b = static_cast<LinuxBuffer*>(buffer);
	}
	// No call can hold it now: each held the registry shared until it was done with the buffer.
	delete b;
}

// Builds the stream into the buffer, which must be empty. Returns 1 when built; on a malformed stream,
// or one without a field, the buffer is left empty.
int VBufLinux_build(void* buffer, const uint16_t* stream, size_t length) {
	return withBuffer(buffer, 0, [&](LinuxBuffer& b) {
		if(b.hasContent()) return 0;
		StreamReader reader(stream, length);
		if(!readFields(reader, b, nullptr) || !b.hasContent()) {
			b.clearBuffer();
			return 0;
		}
		return 1;
	});
}

int VBufLinux_update(void* buffer, const uint16_t* stream, size_t length) {
	return withBuffer(buffer, 0, [&](LinuxBuffer& b) { return applyUpdate(b, stream, length); });
}

// Builds into the empty buffer the field an update batch of a single S and no R replaces with, and
// gives that S's identifier, so that a batch replacing a whole document is built off NVDA's main
// thread and VBufLinux_adopt only takes it there. Returns 1 when built; otherwise the buffer is left
// empty.
int VBufLinux_buildReplacement(void* buffer, const uint16_t* stream, size_t length, int* docHandle, int* ID) {
	if(!docHandle || !ID) return 0;
	return withBuffer(buffer, 0, [&](LinuxBuffer& b) {
		if(b.hasContent()) return 0;
		StreamReader reader(stream, length);
		uint16_t op;
		long long targetDocHandle, targetID;
		if(!reader.op(&op) || op != u'S' || !reader.integer(&targetDocHandle, u',') || !reader.integer(&targetID, u';')) return 0;
		if(!readFields(reader, b, nullptr, true) || !reader.atEnd() || !b.hasContent()) {
			b.clearBuffer();
			return 0;
		}
		*docHandle = (int)targetDocHandle;
		*ID = (int)targetID;
		return 1;
	});
}

// Applies the batch VBufLinux_buildReplacement built into replacement, which replaces the field docHandle,
// ID, when that is the buffer's root: the buffer takes replacement's fields and replacement the
// buffer's old ones, for the caller to destroy off the main thread. Returns 0, changing nothing, when
// the batch replaces anything else, which VBufLinux_update then applies.
int VBufLinux_adopt(void* buffer, void* replacement, int docHandle, int ID) {
	return withBuffer(buffer, 0, [&](LinuxBuffer& b) {
		if(replacement == buffer || !liveBuffers.count(replacement)) return 0;
		auto& other = *static_cast<LinuxBuffer*>(replacement);
		std::lock_guard<std::mutex> guard(other.lock);
		if(!b.adopt(other, docHandle, ID)) return 0;
		b.workingInvalidSubtreesList.clear();
		clampSelection(b);
		return 1;
	});
}

void VBufLinux_free(void* p) {
	free(p);
}

int VBuf_getFieldNodeOffsets(void* buffer, VBufLinux_nodeHandle_t node, int* startOffset, int* endOffset) {
	if(!startOffset || !endOffset) return 0;
	return withBuffer(buffer, 0, [&](LinuxBuffer& b) {
		auto n = reinterpret_cast<VBufStorage_fieldNode_t*>(node);
		if(!b.isNodeInBuffer(n)) return 0;
		return b.getFieldNodeOffsets(n, startOffset, endOffset) ? 1 : 0;
	});
}

int VBuf_isFieldNodeAtOffset(void* buffer, VBufLinux_nodeHandle_t node, int offset) {
	return withBuffer(buffer, 0, [&](LinuxBuffer& b) {
		auto n = reinterpret_cast<VBufStorage_fieldNode_t*>(node);
		if(!b.isNodeInBuffer(n)) return 0;
		return b.isFieldNodeAtOffset(n, offset) ? 1 : 0;
	});
}

int VBuf_locateTextFieldNodeAtOffset(void* buffer, int offset, int* startOffset, int* endOffset, VBufLinux_nodeHandle_t* foundNode) {
	if(!foundNode) return 0;
	*foundNode = 0;
	return withBuffer(buffer, 0, [&](LinuxBuffer& b) {
		*foundNode = (VBufLinux_nodeHandle_t)b.locateTextFieldNodeAtOffset(offset, startOffset, endOffset);
		return *foundNode != 0 ? 1 : 0;
	});
}

int VBuf_locateControlFieldNodeAtOffset(void* buffer, int offset, int* startOffset, int* endOffset, int* docHandle, int* ID, VBufLinux_nodeHandle_t* foundNode) {
	if(!foundNode) return 0;
	*foundNode = 0;
	return withBuffer(buffer, 0, [&](LinuxBuffer& b) {
		*foundNode = (VBufLinux_nodeHandle_t)b.locateControlFieldNodeAtOffset(offset, startOffset, endOffset, docHandle, ID);
		return *foundNode != 0 ? 1 : 0;
	});
}

int VBuf_getControlFieldNodeWithIdentifier(void* buffer, int docHandle, int ID, VBufLinux_nodeHandle_t* foundNode) {
	if(!foundNode) return 0;
	*foundNode = 0;
	return withBuffer(buffer, 0, [&](LinuxBuffer& b) {
		*foundNode = (VBufLinux_nodeHandle_t)b.getControlFieldNodeWithIdentifier(docHandle, ID);
		return *foundNode != 0 ? 1 : 0;
	});
}

int VBuf_getIdentifierFromControlFieldNode(void* buffer, VBufLinux_nodeHandle_t node, int* docHandle, int* ID) {
	return withBuffer(buffer, 0, [&](LinuxBuffer& b) {
		auto n = reinterpret_cast<VBufStorage_fieldNode_t*>(node);
		// Upstream casts without looking; a text field here would be read as a control field.
		auto c = b.isNodeInBuffer(n) ? dynamic_cast<VBufStorage_controlFieldNode_t*>(n) : nullptr;
		if(!c) return 0;
		return b.getIdentifierFromControlFieldNode(c, docHandle, ID) ? 1 : 0;
	});
}

int VBuf_findNodeByAttributes(void* buffer, int offset, int direction, const uint16_t* attribs, size_t attribsLength, const uint16_t* regexp, size_t regexpLength, int* startOffset, int* endOffset, VBufLinux_nodeHandle_t* foundNode) {
	if(!foundNode || !startOffset || !endOffset) return 0;
	*foundNode = 0;
	return withBuffer(buffer, 0, [&](LinuxBuffer& b) {
		std::wstring attribsText = attribs ? std::wstring(attribs, attribs + attribsLength) : std::wstring();
		std::wstring regexpText = regexp ? std::wstring(regexp, regexp + regexpLength) : std::wstring();
		*foundNode = (VBufLinux_nodeHandle_t)b.findNodeByAttributes(offset, (VBufStorage_findDirection_t)direction, attribsText, regexpText, startOffset, endOffset);
		return *foundNode != 0 ? 1 : 0;
	});
}

int VBuf_getSelectionOffsets(void* buffer, int* startOffset, int* endOffset) {
	if(!startOffset || !endOffset) return 0;
	return withBuffer(buffer, 0, [&](LinuxBuffer& b) { return b.getSelectionOffsets(startOffset, endOffset) ? 1 : 0; });
}

int VBuf_setSelectionOffsets(void* buffer, int startOffset, int endOffset) {
	return withBuffer(buffer, 0, [&](LinuxBuffer& b) { return b.setSelectionOffsets(startOffset, endOffset) ? 1 : 0; });
}

int VBuf_getTextLength(void* buffer) {
	return withBuffer(buffer, 0, [&](LinuxBuffer& b) { return b.getTextLength(); });
}

// The text, with markup when asked, as UTF-16 units in memory the caller frees with VBufLinux_free,
// and its length in units; null when empty.
uint16_t* VBuf_getTextInRange(void* buffer, int startOffset, int endOffset, int useMarkup, size_t* length) {
	if(!length) return nullptr;
	*length = 0;
	std::wstring text;
	if(!withBuffer(buffer, false, [&](LinuxBuffer& b) { return b.getTextInRange(startOffset, endOffset, text, useMarkup != 0); })) return nullptr;
	if(text.empty()) return nullptr;
	auto out = static_cast<uint16_t*>(malloc(text.size() * sizeof(uint16_t)));
	if(!out) return nullptr;
	for(size_t i = 0; i < text.size(); ++i) out[i] = (uint16_t)text[i];
	*length = text.size();
	return out;
}

int VBuf_getLineOffsets(void* buffer, int offset, int maxLineLength, int useScreenLayout, int* startOffset, int* endOffset) {
	if(!startOffset || !endOffset) return 0;
	return withBuffer(buffer, 0, [&](LinuxBuffer& b) {
		// Upstream reads through a null node for a negative offset.
		if(offset < 0) return 0;
		return b.getLineOffsets(offset, maxLineLength, useScreenLayout != 0, startOffset, endOffset) ? 1 : 0;
	});
}

}  // extern "C"
