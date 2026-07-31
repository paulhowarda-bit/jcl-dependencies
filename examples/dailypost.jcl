//DAILYPST JOB (FIN),'DAILY POST',CLASS=A,MSGCLASS=X
//*
//* Inline PROC with a symbolic parameter and default, invoked with an
//* override value and a PROC-step override DD. Also an INCLUDE member
//* (a control file pulled from the JCL library by the caller's resolver).
//*
//POSTPRC  PROC ENV=TEST
//RUNPOST  EXEC PGM=DAILYPOST
//TRANIN   DD  DSN=&ENV..FIN.TRANS,DISP=SHR
//LEDGER   DD  DSN=&ENV..FIN.LEDGER,DISP=(MOD,KEEP)
//SYSOUT   DD  SYSOUT=*
//         PEND
//*
//STEP01   EXEC POSTPRC,ENV=PROD
//RUNPOST.AUDIT DD DSN=PROD.FIN.AUDIT,DISP=(NEW,CATLG)
//         INCLUDE MEMBER=FINSTD
//
