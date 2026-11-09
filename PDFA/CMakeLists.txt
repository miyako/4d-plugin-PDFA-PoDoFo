cmake_minimum_required(VERSION 3.15)
project(PDFA LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 17)  # PoDoFo 0.10+ requires C++17
set(CMAKE_C_VISIBILITY_PRESET default)
set(CMAKE_CXX_VISIBILITY_PRESET default)

if(APPLE)
    set(CMAKE_OSX_ARCHITECTURES "arm64")
endif()

if(MSVC)
    # Static CRT (/MT) so the .4DX does not need the VC++ redistributable.
    # Must match the vcpkg triplet (x64-windows-static-release, VCPKG_CRT_LINKAGE static).
    set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")
endif()

set(SDK_DIR "${CMAKE_CURRENT_SOURCE_DIR}/../4D-Plugin-SDK/4D Plugin API")

add_library(PDFA MODULE
    PDFA-4dplugin.cpp
    "${SDK_DIR}/4DPluginAPI.c"
)

target_include_directories(PDFA PRIVATE
    "${SDK_DIR}"
)

if(APPLE)
    set_source_files_properties("${SDK_DIR}/4DPluginAPI.c" PROPERTIES
        COMPILE_FLAGS "-Wno-int-conversion -Wno-multichar")
endif()

# PoDoFo — find via pkg-config or cmake find_package
# Install: brew install podofo (macOS),
#          vcpkg install podofo:x64-windows-static-release --overlay-triplets=vcpkg-triplets (Windows)
# On Windows always use the CMake config package: it carries the transitive
# static dependencies (OpenSSL, libxml2, freetype, ...) that pkg-config would miss.
if(NOT WIN32)
    find_package(PkgConfig QUIET)
endif()
if(PkgConfig_FOUND)
    pkg_check_modules(PODOFO REQUIRED libpodofo)
    target_include_directories(PDFA PRIVATE ${PODOFO_INCLUDE_DIRS})
    target_link_libraries(PDFA PRIVATE ${PODOFO_LIBRARIES})
    target_link_directories(PDFA PRIVATE ${PODOFO_LIBRARY_DIRS})
else()
    find_package(podofo CONFIG REQUIRED)
    target_link_libraries(PDFA PRIVATE podofo::podofo)
endif()

if(APPLE)
    find_library(COREGRAPHICS_FRAMEWORK CoreGraphics)
    find_library(COREFOUNDATION_FRAMEWORK CoreFoundation)
    find_library(COCOA_FRAMEWORK Cocoa)
    target_link_libraries(PDFA PRIVATE
        ${COREGRAPHICS_FRAMEWORK}
        ${COREFOUNDATION_FRAMEWORK}
        ${COCOA_FRAMEWORK}
    )

    set_target_properties(PDFA PROPERTIES
        BUNDLE TRUE
        BUNDLE_EXTENSION "bundle"
        PREFIX ""
        MACOSX_BUNDLE_INFO_PLIST "${CMAKE_CURRENT_SOURCE_DIR}/Info.plist"
    )

    set(PLUGIN_OUTPUT "${CMAKE_CURRENT_SOURCE_DIR}/PDFA-test/Plugins/PDFA.bundle")
    add_custom_command(TARGET PDFA POST_BUILD
        COMMAND ${CMAKE_COMMAND} -E remove_directory "${PLUGIN_OUTPUT}"
        COMMAND ${CMAKE_COMMAND} -E copy_directory "$<TARGET_BUNDLE_DIR:PDFA>" "${PLUGIN_OUTPUT}"
        COMMAND ${CMAKE_COMMAND} -E make_directory "${PLUGIN_OUTPUT}/Contents/Resources"
        COMMAND ${CMAKE_COMMAND} -E copy "${CMAKE_CURRENT_SOURCE_DIR}/manifest.json" "${PLUGIN_OUTPUT}/Contents/Resources/manifest.json"
        COMMAND ${CMAKE_COMMAND} -E copy "${CMAKE_CURRENT_SOURCE_DIR}/resources/sRGB.icc" "${PLUGIN_OUTPUT}/Contents/Resources/sRGB.icc"
    )

elseif(WIN32)
    set_target_properties(PDFA PROPERTIES
        PREFIX ""
        SUFFIX ".4DX"
        LINK_FLAGS "/DEF:${CMAKE_CURRENT_SOURCE_DIR}/PDFA.def"
    )

    target_compile_options(PDFA PRIVATE /W3 /wd4267 /wd4244)

    set(PLUGIN_OUTPUT_DIR "${CMAKE_CURRENT_SOURCE_DIR}/PDFA-test/Plugins/PDFA/Contents")
    add_custom_command(TARGET PDFA POST_BUILD
        COMMAND ${CMAKE_COMMAND} -E make_directory "${PLUGIN_OUTPUT_DIR}/Windows64"
        COMMAND ${CMAKE_COMMAND} -E copy "$<TARGET_FILE:PDFA>" "${PLUGIN_OUTPUT_DIR}/Windows64/PDFA.4DX"
        COMMAND ${CMAKE_COMMAND} -E make_directory "${PLUGIN_OUTPUT_DIR}/Resources"
        COMMAND ${CMAKE_COMMAND} -E copy "${CMAKE_CURRENT_SOURCE_DIR}/manifest.json" "${PLUGIN_OUTPUT_DIR}/Resources/manifest.json"
        COMMAND ${CMAKE_COMMAND} -E copy "${CMAKE_CURRENT_SOURCE_DIR}/resources/sRGB.icc" "${PLUGIN_OUTPUT_DIR}/Resources/sRGB.icc"
    )
endif()
