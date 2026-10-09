//AUTOEDIT JOB (OPS),'AUTOEDIT VARS',CLASS=A,MSGCLASS=X
//*
//* Control-M loads this job's AutoEdit variables from a member when it
//* SUBMITS the job. The directive rides on a comment card so that JES
//* never sees it; %%SSID reaches the JCL only once Control-M has read
//* SITE.CTM.AUTOEDIT(SITEVARS) and substituted it. So the job depends on
//* that member - an autoedit-member row - and each dataset name still
//* carrying %%SSID is flagged with the member beside it, rather than
//* read as a control card nobody could supply.
//*
//* %%INCLIB SITE.CTM.AUTOEDIT %%INCMEM SITEVARS
//*
//UNLDPRC  PROC
//UNLOAD   EXEC PGM=IKJEFT01
//SYSTSIN  DD  DSN=&SSID..CCARDLIB(DSNTEP),DISP=SHR
//SYSIN    DD  DSN=PARM.LIB(UNL&SSID),DISP=SHR
//SYSREC00 DD  DSN=PROD.ACCT.UNLOAD,DISP=(NEW,CATLG,DELETE)
//SYSTSPRT DD  SYSOUT=*
//         PEND
//*
//STEP01   EXEC UNLDPRC,SSID=%%SSID
//
