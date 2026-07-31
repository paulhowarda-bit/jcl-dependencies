//EDVALID  PROC ENV=TEST
//*
//* A cataloged PROC member (bare - it is invoked from elsewhere). Passed
//* to the tool directly, it is analysed with its default symbolics.
//*
//VALIDATE EXEC PGM=EDCHECK
//CARDIN   DD  DSN=&ENV..EDIT.CARDS,DISP=SHR
//GOODOUT  DD  DSN=&ENV..EDIT.GOOD,DISP=(NEW,CATLG,DELETE)
//BADOUT   DD  DSN=&ENV..EDIT.REJECT,DISP=(NEW,CATLG,DELETE)
//SYSOUT   DD  SYSOUT=*
//         PEND
