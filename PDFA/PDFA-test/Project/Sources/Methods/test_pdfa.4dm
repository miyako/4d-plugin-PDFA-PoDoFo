//%attributes = {"invisible":true}
$inputPath:=System folder:C487(Desktop:K41:16)+"test.pdf"
SET CURRENT PRINTER:C787(Generic PDF driver:K47:15)
SET PRINT OPTION:C733(Destination option:K47:7; 3; $inputPath)
OPEN PRINTING JOB:C995
$height:=Print form:C5("TEST"; Form detail:K43:1)
CLOSE PRINTING JOB:C996

var $result : Object
$outputPath:=System folder:C487(Desktop:K41:16)+"test_pdfa3.pdf"

$result:=PDF TO PDFA(\
Convert path system to POSIX:C1106($inputPath); \
Convert path system to POSIX:C1106($outputPath))

If ($result.success)
	TRACE:C157  // inspect result
Else 
	ALERT:C41($result.message)
End if 
