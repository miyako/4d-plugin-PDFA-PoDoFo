#include "4DPluginAPI.h"
#include "PrivateTypes.h"
#include "EntryPoints.h"
#include <cassert>
#include <codecvt>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <locale>
#include <set>
#include <string>
#include <vector>

#include "status_helpers.inc"

static std::set<PA_Unichar*> liveStrings;
static std::wstring_convert<std::codecvt_utf8_utf16<char16_t>, char16_t> utf;
static std::string expectedJson;
static bool allocatedEmpty;
static int parseCalls;
static int objectToken;

extern "C" void PluginMain(PA_long32, PA_PluginParameters) {}

static void host(short selector, EngineBlock* eb) {
    eb->fError = eER_NoErr;
    switch (selector) {
        case EX_CREATE_UNISTRING: {
            const auto* input = reinterpret_cast<const PA_Unichar*>(eb->fHandle);
            size_t length = 0;
            while (input[length]) ++length;
            eb->fUniString1 = {};
            eb->fUniString1.fLength = static_cast<PA_long32>(length);
            if (length || allocatedEmpty) {
                auto* copy = static_cast<PA_Unichar*>(
                    std::malloc((length + 1) * sizeof(PA_Unichar)));
                assert(copy);
                std::memcpy(copy, input, (length + 1) * sizeof(PA_Unichar));
                eb->fUniString1.fString = copy;
                assert(liveStrings.insert(copy).second);
            }
            break;
        }
        case EX_DISPOSE_UNISTRING:
            if (auto* string = eb->fUniString1.fString) {
                assert(liveStrings.erase(string) == 1);
                std::free(string);
            }
            break;
        case EX_CONVERT_CHARSET_TO_CHARSET: {
            std::string bytes;
            if (eb->fParam2 == eVTC_UTF_8 && eb->fParam4 == eVTC_UTF_16) {
                auto converted = utf.from_bytes(
                    static_cast<const char*>(eb->fPtr1),
                    static_cast<const char*>(eb->fPtr1) + eb->fParam1);
                bytes.assign(reinterpret_cast<const char*>(converted.data()),
                             converted.size() * sizeof(char16_t));
            } else {
                assert(eb->fParam2 == eVTC_UTF_16 && eb->fParam4 == eVTC_UTF_8);
                assert(eb->fParam1 % sizeof(PA_Unichar) == 0);
                std::u16string input;
                const auto* data = static_cast<const PA_Unichar*>(eb->fPtr1);
                for (size_t i = 0; i < eb->fParam1 / sizeof(PA_Unichar); ++i)
                    input.push_back(data[i]);
                bytes = utf.to_bytes(input);
            }
            assert(bytes.size() <= static_cast<size_t>(eb->fParam3));
            std::memcpy(eb->fPtr2, bytes.data(), bytes.size());
            // The SDK forwards a byte count, not a UTF-16 code-unit count.
            eb->fParam3 = bytes.size();
            break;
        }
        case EX_HANDLE_MANAGER:
            switch (eb->fParam1) {
                case 1:
                    eb->fHandle = static_cast<PA_Handle>(std::malloc(sizeof(char*)));
                    assert(eb->fHandle);
                    *eb->fHandle = static_cast<char*>(std::malloc(eb->fParam2));
                    assert(*eb->fHandle);
                    break;
                case 2:
                    std::free(*eb->fHandle);
                    std::free(eb->fHandle);
                    break;
                case 5: eb->fParam3 = reinterpret_cast<sLONG_PTR>(*eb->fHandle); break;
                case 6: break;
                default: assert(false);
            }
            break;
        case EX_EXECUTE_COMMAND_BY_ID: {
            assert(eb->fParam1 == 1218 && eb->fParam2 == 2);
            auto** arguments = static_cast<PA_Variable**>(eb->fPtr1);
            assert(arguments[0]->fType == eVK_Unistring);
            assert(arguments[1]->fType == eVK_Longint);
            assert(arguments[1]->uValue.fLongint == eVK_Object);
            auto string = arguments[0]->uValue.fString;
            PA_Unichar* buffer = string.fString;
            assert(liveStrings.count(buffer) == 1);
            std::u16string text;
            for (PA_long32 i = 0; i < string.fLength; ++i)
                text.push_back(string.fString[i]);
            assert(utf.to_bytes(text) == expectedJson);
            // JSON Parse consumes the text synchronously, not its allocation.
            auto* result = static_cast<PA_Variable*>(eb->fPtr2);
            result->fType = eVK_Object;
            result->uValue.fObject = reinterpret_cast<PA_ObjectRef>(&objectToken);
            ++parseCalls;
            break;
        }
        default: assert(false);
    }
}

int main() {
    static_assert(sizeof(PA_Unichar) == sizeof(char16_t));
    gCall4D = host;
    const std::string fixtures[] = {
        "", "ASCII", "caf\xc3\xa9 \xe6\x97\xa5\xe6\x9c\xac",
        "emoji \xf0\x9f\x98\x80", "quote\" slash\\\n"
    };
    for (bool emptyRepresentation : {false, true}) {
        allocatedEmpty = emptyRepresentation;
        for (const auto& fixture : fixtures) {
            auto converted = utf.from_bytes(fixture);
            std::vector<PA_Unichar> text(converted.begin(), converted.end());
            text.push_back(0);
            PA_Unistring string = PA_CreateUnistring(text.data());
            assert(uniToUtf8(&string) == fixture);
            PA_DisposeUnistring(&string);
            assert(liveStrings.empty());
        }
        assert(uniToUtf8(nullptr).empty());
        for (int iteration = 0; iteration < 100; ++iteration) {
            for (int code : {0, -1, -2, -3, -10, -11}) {
                for (const auto& fixture : fixtures) {
                    std::string escaped;
                    for (char c : fixture) {
                        if (c == '"') escaped += "\\\"";
                        else if (c == '\\') escaped += "\\\\";
                        else if (c == '\n') escaped += "\\n";
                        else escaped += c;
                    }
                    expectedJson = "{\"success\":" + std::string(code == 0 ? "true" : "false")
                        + ",\"code\":" + std::to_string(code)
                        + ",\"message\":\"" + escaped + "\"}";
                    PA_ObjectRef result = nullptr;
                    PluginBlock parameters = {};
                    parameters.fResult = &result;
                    returnStatus(&parameters, code, fixture);
                    assert(result == reinterpret_cast<PA_ObjectRef>(&objectToken));
                    assert(liveStrings.empty() && "returnStatus leaked its JSON string");
                }
            }
        }
    }
    assert(parseCalls == 6000);
    std::puts("6000 status returns: exact text, one disposal, returned object preserved");
}
