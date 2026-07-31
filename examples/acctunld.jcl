//ACCTUNLD JOB (ACCT),'DAILY UNLOAD',CLASS=A,MSGCLASS=X
//*
//* Daily account unload + sort. STEP01 runs the SQLUNLD COBOL program
//* (examples/sqlunld.cbl) whose OUT-FILE is ASSIGNed to ddname OUTDD -
//* so this JCL is exactly what resolves that program's OUTDD -> a DSN.
//*
//         SET HLQ=PROD
//*
//STEP01   EXEC PGM=SQLUNLD
//STEPLIB  DD  DSN=&HLQ..LOADLIB,DISP=SHR
//OUTDD    DD  DSN=&HLQ..ACCT.UNLOAD(+1),
//             DISP=(NEW,CATLG,DELETE),
//             SPACE=(CYL,(10,5),RLSE)
//SYSOUT   DD  SYSOUT=*
//*
//STEP02   EXEC PGM=SORT
//SORTIN   DD  DSN=&HLQ..ACCT.UNLOAD(+1),DISP=SHR
//SORTOUT  DD  DSN=&HLQ..ACCT.SORTED,DISP=(NEW,CATLG,DELETE)
//SYSIN    DD  *
  SORT FIELDS=(1,5,CH,A)
  INCLUDE COND=(28,1,CH,EQ,C'A')
  OUTREC BUILD=(1,5,6,20,28,8)
/*
//SYSPRINT DD  SYSOUT=*
//
