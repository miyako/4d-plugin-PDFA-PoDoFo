//%attributes = {"invisible":true}
var $inputPath : Text
var $outputPath : Text
var $result : Object

// Create a minimal test PDF using PRINT SETTINGS
$inputPath:=Temporary folder+"test_input.pdf"
$outputPath:=Temporary folder+"test_output_pdfa3.pdf"

// For CI: we need a sample PDF. Create one via BLOB.
// If no test PDF exists, just verify the plugin loads and returns proper error
$result:=PDF TO PDFA($inputPath; $outputPath)

// Plugin should return an error object (file not found)
ASSERT($result#Null; "PDF TO PDFA should return an object")
ASSERT($result.success=False; "Should fail with non-existent input")
ASSERT($result.code#0; "Error code should be non-zero")

// Test with empty params
$result:=PDF TO PDFA(""; "")
ASSERT($result#Null; "Should return object for empty params")
ASSERT($result.success=False; "Should fail with empty params")

KILL WORKER
