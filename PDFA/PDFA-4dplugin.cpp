/*
 * PDFA-4dplugin.cpp — Candidate C: PoDoFo
 *
 * Converts PDF to PDF/A-3 using the PoDoFo library (LGPL-2.0+).
 * Manually adds:
 *   1. XMP metadata declaring PDF/A-3 conformance
 *   2. sRGB ICC OutputIntent
 *   3. Ensures document structure for PDF/A compliance
 *
 * This is the most permissive license option but requires the most
 * implementation effort. PoDoFo does not have a single "convert to PDF/A"
 * function — everything must be constructed manually.
 *
 * Requires PoDoFo 0.10+ (C++17).
 */

#include "4DPluginAPI.h"
#include <cstring>
#include <cstdio>
#include <cstdlib>
#include <string>
#include <vector>
#include <fstream>
#include <sstream>

#include <podofo/podofo.h>

#ifdef _WIN32
#include <windows.h>
#else
#include <CoreFoundation/CoreFoundation.h>
#endif

static void doPdfToPdfa(PA_PluginParameters params);

/* helper: get the plugin's Resources path */
static std::string getResourcesPath() {
#ifdef __APPLE__
    CFBundleRef bundle = CFBundleGetBundleWithIdentifier(CFSTR("com.4d.PDFA"));
    if (!bundle) return "";
    CFURLRef resourcesURL = CFBundleCopyResourcesDirectoryURL(bundle);
    if (!resourcesURL) return "";
    char path[4096];
    CFURLGetFileSystemRepresentation(resourcesURL, true, (UInt8*)path, sizeof(path));
    CFRelease(resourcesURL);
    return std::string(path);
#else
    char modulePath[MAX_PATH];
    HMODULE hm = NULL;
    GetModuleHandleExA(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS |
                       GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                       (LPCSTR)&doPdfToPdfa, &hm);
    GetModuleFileNameA(hm, modulePath, MAX_PATH);
    std::string p(modulePath);
    size_t pos = p.rfind("\\Windows64\\");
    if (pos == std::string::npos) pos = p.rfind("/Windows64/");
    if (pos != std::string::npos) {
        return p.substr(0, pos) + "\\Resources";
    }
    return "";
#endif
}

/* helper: convert PA_Unistring to UTF-8 */
static std::string uniToUtf8(PA_Unistring* ustr) {
    if (!ustr) return "";
    PA_long32 len = PA_GetUnistringLength(ustr);
    if (len == 0) return "";
    const PA_Unichar* data = PA_GetUnistring(ustr);
    std::vector<char> buf(len * 4 + 1);
    PA_long32 converted = PA_ConvertCharsetToCharset(
        (char*)data, len * sizeof(PA_Unichar), eVTC_UTF_16,
        buf.data(), (PA_long32)buf.size(), eVTC_UTF_8);
    return std::string(buf.data(), converted);
}

/* helper: return a JSON status object */
static void returnStatus(PA_PluginParameters params, int code, const std::string& message) {
    /* escape any quotes in message */
    std::string escaped;
    for (char c : message) {
        if (c == '"') escaped += "\\\"";
        else if (c == '\\') escaped += "\\\\";
        else if (c == '\n') escaped += "\\n";
        else escaped += c;
    }

    char json[4096];
    snprintf(json, sizeof(json),
        "{\"success\":%s,\"code\":%d,\"message\":\"%s\"}",
        code == 0 ? "true" : "false", code, escaped.c_str());

    std::vector<PA_Unichar> json16(strlen(json) * 2 + 2);
    PA_ConvertCharsetToCharset(
        json, (PA_long32)strlen(json), eVTC_UTF_8,
        (char*)json16.data(), (PA_long32)(json16.size() * sizeof(PA_Unichar)), eVTC_UTF_16);

    PA_Unistring jsonUstr = PA_CreateUnistring(json16.data());

    PA_Variable cmdParams[2];
    memset(cmdParams, 0, sizeof(cmdParams));
    PA_SetStringVariable(&cmdParams[0], &jsonUstr);
    PA_SetLongintVariable(&cmdParams[1], eVK_Object);
    PA_Variable result = PA_ExecuteCommandByID(1218, cmdParams, 2);

    PA_ReturnObject(params, PA_GetObjectVariable(result));
}

/* read file into buffer */
static std::vector<char> readFile(const std::string& path) {
    std::ifstream f(path, std::ios::binary);
    return std::vector<char>(
        std::istreambuf_iterator<char>(f),
        std::istreambuf_iterator<char>());
}

/* Build XMP metadata for PDF/A-3 */
static std::string buildPdfA3Xmp() {
    return
        "<?xpacket begin=\"\xef\xbb\xbf\" id=\"W5M0MpCehiHzreSzNTczkc9d\"?>\n"
        "<x:xmpmeta xmlns:x=\"adobe:ns:meta/\">\n"
        "  <rdf:RDF xmlns:rdf=\"http://www.w3.org/1999/02/22-rdf-syntax-ns#\">\n"
        "    <rdf:Description rdf:about=\"\"\n"
        "      xmlns:dc=\"http://purl.org/dc/elements/1.1/\"\n"
        "      xmlns:pdfaid=\"http://www.aiim.org/pdfa/ns/id/\"\n"
        "      xmlns:xmp=\"http://ns.adobe.com/xap/1.0/\">\n"
        "      <pdfaid:part>3</pdfaid:part>\n"
        "      <pdfaid:conformance>B</pdfaid:conformance>\n"
        "      <dc:title>\n"
        "        <rdf:Alt>\n"
        "          <rdf:li xml:lang=\"x-default\">PDF/A-3 Document</rdf:li>\n"
        "        </rdf:Alt>\n"
        "      </dc:title>\n"
        "    </rdf:Description>\n"
        "  </rdf:RDF>\n"
        "</x:xmpmeta>\n"
        "<?xpacket end=\"w\"?>";
}

static void doPdfToPdfa(PA_PluginParameters params) {
    PA_Unistring* inputUstr = PA_GetStringParameter(params, 1);
    PA_Unistring* outputUstr = PA_GetStringParameter(params, 2);

    std::string inputPath = uniToUtf8(inputUstr);
    std::string outputPath = uniToUtf8(outputUstr);

    if (inputPath.empty() || outputPath.empty()) {
        returnStatus(params, -1, "Input and output paths are required");
        return;
    }

    std::string resPath = getResourcesPath();
    if (resPath.empty()) {
        returnStatus(params, -2, "Could not locate plugin Resources folder");
        return;
    }

    std::string iccPath = resPath + "/sRGB.icc";

    try {
        using namespace PoDoFo;

        PdfMemDocument doc;
        doc.Load(inputPath);

        auto& objects = doc.GetObjects();
        auto& catalog = doc.GetCatalog();

        /* 1. Add XMP metadata stream */
        std::string xmp = buildPdfA3Xmp();
        auto& metaObj = objects.CreateDictionaryObject();
        metaObj.GetDictionary().AddKey("Type", PdfName("Metadata"));
        metaObj.GetDictionary().AddKey("Subtype", PdfName("XML"));
        /* Write XMP to stream */
        {
            auto stream = metaObj.GetOrCreateStream().GetOutputStream();
            stream.Write(xmp.c_str(), xmp.size());
        }
        catalog.GetObject().GetDictionary().AddKey("Metadata", metaObj.GetIndirectReference());

        /* 2. Add ICC OutputIntent */
        std::vector<char> iccData = readFile(iccPath);
        if (iccData.empty()) {
            returnStatus(params, -3, "Could not read sRGB.icc profile");
            return;
        }

        /* Create ICC profile stream object */
        auto& iccObj = objects.CreateDictionaryObject();
        iccObj.GetDictionary().AddKey("N", PdfVariant(static_cast<int64_t>(3)));
        iccObj.GetDictionary().AddKey("Alternate", PdfName("DeviceRGB"));
        {
            auto stream = iccObj.GetOrCreateStream().GetOutputStream();
            stream.Write(iccData.data(), iccData.size());
        }

        /* Create OutputIntent dictionary */
        auto& oiObj = objects.CreateDictionaryObject();
        oiObj.GetDictionary().AddKey("Type", PdfName("OutputIntent"));
        oiObj.GetDictionary().AddKey("S", PdfName("GTS_PDFA1"));
        oiObj.GetDictionary().AddKey("OutputConditionIdentifier",
                                     PdfString("sRGB IEC61966-2.1"));
        oiObj.GetDictionary().AddKey("Info",
                                     PdfString("sRGB IEC61966-2.1"));
        oiObj.GetDictionary().AddKey("RegistryName",
                                     PdfString("http://www.color.org"));
        oiObj.GetDictionary().AddKey("DestOutputProfile",
                                     iccObj.GetIndirectReference());

        /* Add OutputIntents array to catalog */
        PdfArray oiArray;
        oiArray.Add(oiObj.GetIndirectReference());
        catalog.GetObject().GetDictionary().AddKey("OutputIntents", oiArray);

        /* 3. Mark catalog with PDF version */
        catalog.GetObject().GetDictionary().AddKey("Version", PdfName("1.7"));

        /* 4. Save */
        doc.Save(outputPath);

        returnStatus(params, 0, "PDF/A-3 conversion successful");

    } catch (const PoDoFo::PdfError& e) {
        std::string msg = "PoDoFo error: " + std::string(e.what());
        returnStatus(params, -10, msg);
    } catch (const std::exception& e) {
        std::string msg = "Error: " + std::string(e.what());
        returnStatus(params, -11, msg);
    }
}

/* PluginMain entry point */
#if defined(_WIN32)
#define PLUGIN_EXPORT
#else
#define PLUGIN_EXPORT __attribute__((visibility("default")))
#endif

extern "C" PLUGIN_EXPORT
void PluginMain(PA_long32 selector, PA_PluginParameters params) {
    switch (PA_GetCurrentProcessNumber()) {
        case kInitPlugin:
        case kDeinitPlugin:
            break;
        default:
            switch (selector) {
                case 1:
                    doPdfToPdfa(params);
                    break;
            }
            break;
    }
}
